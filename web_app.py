#!/usr/bin/env python3
"""
iFlight Neo Wars Deployment - Web Application
Real-time deployment with Server-Sent Events and File Size Tracking
"""

from flask import Flask, render_template, request, jsonify, Response
import json
import os
import threading
import queue
from datetime import datetime
from dotenv import load_dotenv
import time
from scp import SCPClient
from deployment_automation import (
    DeploymentConfig, SSHClient, calculate_local_md5, 
    get_remote_md5, load_config_from_json
)
import sys
import subprocess

# Force install the scp module if it is missing
try:
    import scp
except ImportError:
    print("SCP module missing. Installing it now...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "scp"])
    import scp

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'iflight-deployment-secret-key'

# Message queues for SSE - one per client
clients = []

# Global state
deployment_state = {
    'running': False,
    'cancelled': False,
    'step': None,
    'progress': 0,
    'logs': [],
    'current_file': None,
    'total_files': 0,
    'completed_files': 0,
    'file_sizes': {}
}


def format_size(size_bytes):
    """Format bytes to human readable size"""
    if size_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} TB"


def create_transfer_callback(war_prefix, war_name, total_size, operation='upload'):
    """Create a callback function for file transfer progress with optimized throttling"""
    import time
    last_update = [0]  # Bytes transferred at last update
    last_time = [0]    # Timestamp of last update
    
    def callback(transferred, total):
        current_time = time.time()
        
        # Update only if: transferred >= 2MB since last update OR 1 second elapsed OR transfer complete
        bytes_since_update = transferred - last_update[0]
        time_since_update = current_time - last_time[0]
        
        if bytes_since_update >= 2097152 or time_since_update >= 1.0 or transferred == total:
            last_update[0] = transferred
            last_time[0] = current_time
            percent = (transferred / total * 100) if total > 0 else 0
            
            # Update file size info with transfer progress
            if war_prefix not in deployment_state['file_sizes']:
                deployment_state['file_sizes'][war_prefix] = {
                    'name': war_name,
                    'source_size': 0,
                    'target_size': 0,
                    'status': 'pending',
                    'transfer_progress': 0,
                    'transferred': 0
                }
            
            deployment_state['file_sizes'][war_prefix].update({
                'transfer_progress': percent,
                'transferred': transferred,
                'total_size': total,
                'status': operation
            })
            
            # Broadcast update (no logging to reduce overhead)
            broadcast_message('file_size', deployment_state['file_sizes'])
    
    return callback


def fast_sftp_download(sftp, remote_path, local_path, war_prefix, war_name, callback=None):
    """High-speed SFTP download using prefetch + streaming MD5 (single-pass).
    
    Returns the MD5 hex digest computed during download, eliminating the need
    for a separate calculate_local_md5() call afterward.
    """
    import hashlib
    file_size = sftp.stat(remote_path).st_size
    md5_hash = hashlib.md5()
    transferred = 0
    
    with sftp.open(remote_path, 'rb') as remote_file:
        # Enable read-ahead prefetching — use default buffer size for better stability
        remote_file.prefetch()
        remote_file.MAX_REQUEST_SIZE = 65536  # 64 KB per request (default 32 KB)
        
        with open(local_path, 'wb') as local_file:
            while True:
                data = remote_file.read(1048576)  # 1 MB read chunks
                if not data:
                    break
                local_file.write(data)
                md5_hash.update(data)
                transferred += len(data)
                if callback:
                    callback(transferred, file_size)
    
    return md5_hash.hexdigest()


def sftp_upload_optimized(ssh, local_path, remote_path, war_prefix, war_name, use_scp=False):
    """Optimized upload with SCP or SFTP based on config"""
    file_size = os.path.getsize(local_path)
    
    # Try SCP if enabled (20-40% faster but needs proper server support)
    if use_scp:
        try:
            from scp import SCPClient
            
            log_message(f"  🚀 SCP Upload {format_size(file_size)}...", 'info')
            
            # Progress tracking
            transferred = [0]
            last_update = [time.time()]
            
            def scp_progress(filename, size, sent):
                current_time = time.time()
                if sent - transferred[0] >= 2097152 or current_time - last_update[0] >= 1.0 or sent == size:
                    transferred[0] = sent
                    last_update[0] = current_time
                    percent = (sent / size * 100) if size > 0 else 0
                    
                    deployment_state['file_sizes'][war_prefix].update({
                        'transfer_progress': percent,
                        'transferred': sent,
                        'total_size': size,
                        'status': 'uploading'
                    })
                    broadcast_message('file_size', deployment_state['file_sizes'])
            
            start_time = time.time()
            
            # Use existing SSH transport for SCP
            with SCPClient(ssh.client.get_transport(), progress=scp_progress, socket_timeout=60.0) as scp:
                scp.put(local_path, remote_path)
            
            elapsed = time.time() - start_time
            speed = file_size / elapsed / 1024 / 1024
            log_message(f"  ✓ SCP Upload completed in {elapsed:.1f}s ({speed:.1f} MB/s)", 'success')
            return
            
        except ImportError:
            log_message(f"  ⚠ SCP module not available (pip install scp), falling back to SFTP", 'warning')
        except Exception as e:
            log_message(f"  ⚠ SCP failed: {str(e)}, falling back to SFTP (slower)", 'warning')
    
    # Standard SFTP upload (stable fallback)
    sftp = ssh.get_sftp()
    log_message(f"  ⬆ Uploading {format_size(file_size)}...", 'info')
    callback = create_transfer_callback(war_prefix, war_name, file_size, 'uploading')
    
    start_time = time.time()
    
    # ---> THE SPEED FIX IS HERE: Added confirm=False <---
    sftp.put(local_path, remote_path, callback=callback, confirm=False)
    elapsed = time.time() - start_time
    
    speed = file_size / elapsed / 1024 / 1024
    log_message(f"  ✓ Uploaded in {elapsed:.1f}s ({speed:.1f} MB/s)", 'success')


def sftp_download_optimized(ssh, remote_path, local_path, war_prefix, war_name, use_scp=False):
    """Optimized download using prefetch + streaming MD5 (SCP fallback available)"""
    
    # Get file size for progress tracking
    sftp = ssh.get_sftp()
    file_size = sftp.stat(remote_path).st_size
    
    # Try SCP if enabled (20-40% faster but needs proper server support)  
    if use_scp:
        try:
            from scp import SCPClient
            
            sftp.close()  # Close SFTP before using SCP
            log_message(f"  🚀 SCP Download {format_size(file_size)}...", 'info')
            
            # Progress tracking
            transferred = [0]
            last_update = [time.time()]
            
            def scp_progress(filename, size, sent):
                current_time = time.time()
                if sent - transferred[0] >= 2097152 or current_time - last_update[0] >= 1.0 or sent == size:
                    transferred[0] = sent
                    last_update[0] = current_time
                    percent = (sent / size * 100) if size > 0 else 0
                    
                    deployment_state['file_sizes'][war_prefix].update({
                        'transfer_progress': percent,
                        'transferred': sent,
                        'total_size': size,
                        'status': 'downloading'
                    })
                    broadcast_message('file_size', deployment_state['file_sizes'])
            
            start_time = time.time()
            
            # Use existing SSH transport for SCP
            with SCPClient(ssh.client.get_transport(), progress=scp_progress, socket_timeout=60.0) as scp:
                scp.get(remote_path, local_path)
            
            elapsed = time.time() - start_time
            speed = file_size / elapsed / 1024 / 1024
            log_message(f"  ✓ SCP Download completed in {elapsed:.1f}s ({speed:.1f} MB/s)", 'success')
            return None  # No streaming MD5 with SCP, caller should compute separately
            
        except ImportError:
            log_message(f"  ⚠ SCP module not available, falling back to SFTP", 'warning')
            sftp = ssh.get_sftp()  # Reopen SFTP
        except Exception as e:
            log_message(f"  ⚠ SCP failed: {str(e)}, using SFTP", 'warning')
            sftp = ssh.get_sftp()  # Reopen SFTP
    
    # Fast SFTP download with prefetch + streaming MD5
    log_message(f"  ⬇ Downloading {format_size(file_size)} (prefetch + streaming MD5)...", 'info')
    callback = create_transfer_callback(war_prefix, war_name, file_size, 'downloading')
    
    start_time = time.time()
    local_md5 = fast_sftp_download(sftp, remote_path, local_path, war_prefix, war_name, callback)
    elapsed = time.time() - start_time
    
    speed = file_size / elapsed / 1024 / 1024
    log_message(f"  ✓ Downloaded in {elapsed:.1f}s ({speed:.1f} MB/s)", 'success')
    return local_md5  # Return pre-computed MD5 to skip separate verification pass


def broadcast_message(msg_type, data):
    """Send message to all connected clients"""
    message = {'type': msg_type, 'data': data}
    dead_clients = []
    for q in clients:
        try:
            q.put_nowait(message)
        except:
            dead_clients.append(q)
    for q in dead_clients:
        clients.remove(q)


def log_message(message, level='info', file_info=None):
    """Add log message and broadcast to clients"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    log_entry = {
        'timestamp': timestamp,
        'level': level,
        'message': message,
        'file_info': file_info
    }
    deployment_state['logs'].append(log_entry)
    broadcast_message('log', log_entry)


def update_progress(progress, current_file=None):
    """Update progress and broadcast to clients"""
    deployment_state['progress'] = progress
    if current_file:
        deployment_state['current_file'] = current_file
    broadcast_message('progress', {
        'progress': progress,
        'current_file': current_file,
        'completed': deployment_state['completed_files'],
        'total': deployment_state['total_files'],
        'file_sizes': deployment_state['file_sizes']
    })


def update_file_size(war_prefix, war_name, source_size=None, target_size=None, status=None):
    """Update and broadcast file size info"""
    if war_prefix not in deployment_state['file_sizes']:
        deployment_state['file_sizes'][war_prefix] = {
            'name': war_name,
            'source_size': 0,
            'target_size': 0,
            'status': 'pending'
        }
    
    if source_size is not None:
        deployment_state['file_sizes'][war_prefix]['source_size'] = source_size
    if target_size is not None:
        deployment_state['file_sizes'][war_prefix]['target_size'] = target_size
    if status is not None:
        deployment_state['file_sizes'][war_prefix]['status'] = status
    
    broadcast_message('file_size', deployment_state['file_sizes'])


def get_remote_file_size(ssh, file_path):
    """Get size of remote file in bytes"""
    stdin, stdout, stderr = ssh.client.exec_command(f"stat -c%s {file_path} 2>/dev/null || echo 0")
    try:
        return int(stdout.read().decode().strip())
    except:
        return 0


def format_size_human(size_bytes):
    """Convert bytes to human readable format (e.g., 376M)"""
    if size_bytes == 0:
        return "0B"
    units = ['B', 'K', 'M', 'G', 'T']
    unit_index = 0
    size = float(size_bytes)
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.0f}{units[unit_index]}"


def deploy_step1(config, selected_wars):
    """Step 1: Download selected WAR files with parallel threading"""
    try:
        log_message("═" * 50, 'info')
        log_message("STEP 1: Package and Download to Local", 'success')
        log_message("═" * 50, 'info')
        
        os.makedirs(config.LOCAL_DOWNLOAD_PATH, exist_ok=True)
        
        deployment_state['total_files'] = len(selected_wars)
        deployment_state['completed_files'] = 0
        deployment_state['file_sizes'] = {}
        
        # Use ThreadPoolExecutor for parallel downloads (max 3 concurrent)
        max_workers = min(3, len(selected_wars))
        log_message(f"🚀 Starting parallel download with {max_workers} threads", 'info')
        
        download_lock = threading.Lock()
        completed_count = [0]
        errors = []
        
        def download_single_war(war_prefix, idx):
            """Download single WAR file in thread"""
            try:
                war_file = f"{war_prefix}-{config.VERSION}.war"
                tar_file = f"{war_prefix}-{config.VERSION}.tar"
                war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
                
                # Each thread needs its own SSH connection
                ssh = SSHClient(config.SOURCE_SERVER, config.SOURCE_USER, config.SOURCE_PASSWORD)
                ssh.client = ssh.connect().client
                
                with download_lock:
                    log_message(f"[{idx}/{len(selected_wars)}] {war_name}", 'info')
                    update_file_size(war_prefix, war_name, status='processing')
                
                source_wars_dir = f"{config.SOURCE_PATH}Wars"
                remote_war = f"{source_wars_dir}/{war_file}"
                remote_tar = f"/tmp/{tar_file}_{threading.current_thread().ident}"
                local_tar = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)
                
                # Get source WAR file size
                source_war_size = get_remote_file_size(ssh, remote_war)
                with download_lock:
                    update_file_size(war_prefix, war_name, source_size=source_war_size)
                    log_message(f"  📦 {war_name}: Source WAR {format_size(source_war_size)}", 'info')
                
                # Package without gzip (WAR files are already ZIP-compressed)
                with download_lock:
                    log_message(f"  ⚙ {war_name}: Packaging (no gzip)...", 'info')
                stdin, stdout, stderr = ssh.client.exec_command(
                    f"cd {source_wars_dir} && tar -cf {remote_tar} {war_file}"
                )
                stdout.channel.recv_exit_status()
                
                # Download with prefetch + streaming MD5
                use_scp = getattr(config, 'USE_SCP', False)
                local_md5 = sftp_download_optimized(ssh, remote_tar, local_tar, war_prefix, war_name, use_scp)
                
                # Verify — use streaming MD5 if available, otherwise compute separately
                with download_lock:
                    log_message(f"  🔐 {war_name}: Verifying MD5...", 'info')
                remote_md5 = get_remote_md5(ssh, remote_tar)
                if local_md5 is None:
                    # SCP path — need to compute MD5 separately
                    local_md5 = calculate_local_md5(local_tar)
                
                if remote_md5 and local_md5 == remote_md5:
                    with download_lock:
                        log_message(f"  ✓ {war_name}: Integrity verified", 'success')
                        update_file_size(war_prefix, war_name, status='downloaded')
                else:
                    with download_lock:
                        log_message(f"  ⚠ {war_name}: Checksum mismatch!", 'warning')
                        update_file_size(war_prefix, war_name, status='warning')
                
                # Cleanup
                ssh.client.exec_command(f"rm -f {remote_tar}")
                ssh.close()
                
                with download_lock:
                    completed_count[0] += 1
                    deployment_state['completed_files'] = completed_count[0]
                    update_progress(completed_count[0] / len(selected_wars) * 100, war_name)
                
                return True
                
            except Exception as e:
                with download_lock:
                    errors.append(f"{war_name}: {str(e)}")
                    log_message(f"✗ {war_name} failed: {str(e)}", 'error')
                return False
        
        # Execute downloads in parallel
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(download_single_war, war_prefix, idx): war_prefix 
                for idx, war_prefix in enumerate(selected_wars, 1)
                if not deployment_state.get('cancelled')
            }
            
            for future in as_completed(futures):
                if deployment_state.get('cancelled'):
                    log_message("⚠ Deployment cancelled", 'warning')
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
        
        if errors:
            log_message(f"⚠ {len(errors)} download(s) had errors", 'warning')
            for error in errors:
                log_message(f"  • {error}", 'error')
        
        log_message("═" * 50, 'info')
        log_message("✓ STEP 1 COMPLETED!", 'success')
        log_message(f"📊 Downloaded {completed_count[0]}/{len(selected_wars)} files", 'info')
        return len(errors) == 0
        
    except Exception as e:
        log_message(f"✗ Step 1 failed: {str(e)}", 'error')
        return False


def deploy_step2(config, selected_wars):
    """Step 2: Fully Distributed Upload across Multiple PAMs and Target Servers"""
    try:
        log_message("═" * 50, 'info')
        log_message("STEP 2: Fully Distributed Upload & Extract", 'success')
        log_message("═" * 50, 'info')
        
        # Safely get routes, with a fallback to the single primary server if routes are missing
        target_routes = getattr(config, 'TARGET_ROUTES', [])
        if not target_routes:
            target_routes = [{'host': config.TARGET_SERVER, 'username': config.TARGET_USER}]
            
        log_message(f"🌐 Distributing load across {len(target_routes)} unique network routes...", 'info')

        missing_files = []
        for war_prefix in selected_wars:
            tar_file = f"{war_prefix}-{config.VERSION}.tar"
            local_tar = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)
            if not os.path.exists(local_tar):
                missing_files.append(tar_file)
        
        if missing_files:
            log_message(f"✗ Missing files for version {config.VERSION}!", 'error')
            return False
            
        deployment_state['total_files'] = len(selected_wars)
        deployment_state['completed_files'] = 0
        
        # --- PRE-CHECK ---
        log_message(f"  🔗 Connecting to primary node for initial directory setup...", 'info')
        primary_route = target_routes[0]
        setup_ssh = SSHClient(primary_route['host'], primary_route['username'], config.TARGET_PASSWORD)
        setup_ssh.client = setup_ssh.connect().client
        setup_ssh.client.exec_command(f"mkdir -p {config.TARGET_EXTRACT_PATH}")
        setup_ssh.close()

        # Increase parallel workers for faster uploads (max 10 concurrent)
        # Using more threads for uploads since it's often the bottleneck
        max_workers = min(10, len(selected_wars))
        log_message(f"🚀 Starting parallel upload with {max_workers} threads", 'info')
        
        upload_lock = threading.Lock()
        completed_count = [0]
        errors = []

        def upload_single_war(item):
            """Upload and extract single WAR file using a specifically assigned route"""
            idx, war_prefix = item
            try:
                war_file = f"{war_prefix}-{config.VERSION}.war"
                tar_file = f"{war_prefix}-{config.VERSION}.tar"
                war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
                
                # Round-robin assignment: Route 1, Route 2, Route 3, Route 1...
                assigned_route = target_routes[idx % len(target_routes)]
                pam_host = assigned_route['host'].split('.')[0] # e.g., PAM_NV or PAM_TYO
                target_ip = assigned_route['username'].split('%')[-1]
                
                with upload_lock:
                    log_message(f"[{idx+1}/{len(selected_wars)}] {war_name} -> {pam_host} -> {target_ip}", 'info')
                    update_file_size(war_prefix, war_name, status='uploading')
                
                # Connect using the unique route for this specific file
                ssh = SSHClient(assigned_route['host'], assigned_route['username'], config.TARGET_PASSWORD)
                ssh.client = ssh.connect().client
                
                # Transport tuning is now handled by SSHClient.connect()
                # No need for duplicate tuning here
                
                local_tar = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)
                target_tar_path = f"/tmp/{tar_file}_{threading.current_thread().ident}"
                
                # Upload using SCP
                sftp_upload_optimized(ssh, local_tar, target_tar_path, war_prefix, war_name, use_scp=True)
                
                # Verify
                local_md5 = calculate_local_md5(local_tar)
                remote_md5 = get_remote_md5(ssh, target_tar_path)
                
                if not remote_md5 or local_md5 != remote_md5:
                    with upload_lock:
                        log_message(f"  ⚠ {war_name} ({pam_host}): MD5 mismatch!", 'warning')
                
                # Extract
                with upload_lock:
                    log_message(f"  📂 {war_name}: Extracting via {target_ip}...", 'info')
                    update_file_size(war_prefix, war_name, status='extracting')
                
                stdin, stdout, stderr = ssh.client.exec_command(
                    f"cd {config.TARGET_EXTRACT_PATH} && tar -xf {target_tar_path}",
                    timeout=300
                )
                
                if stdout.channel.recv_exit_status() != 0:
                    raise Exception(f"Extraction failed: {stderr.read().decode().strip()}")
                
                # Cleanup
                ssh.client.exec_command(f"rm -f {target_tar_path}")
                
                # Get final size
                deployed_war = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
                final_size = get_remote_file_size(ssh, deployed_war)
                
                with upload_lock:
                    log_message(f"  ✓ {war_name} ({target_ip}): Done ({format_size(final_size)})", 'success')
                    update_file_size(war_prefix, war_name, target_size=final_size, status='extracted')
                    completed_count[0] += 1
                    deployment_state['completed_files'] = completed_count[0]
                    update_progress(completed_count[0] / len(selected_wars) * 100, war_name)
                
                ssh.close()
                return True
                
            except Exception as e:
                with upload_lock:
                    errors.append(f"{war_prefix}: {str(e)}")
                    log_message(f"  ✗ {war_prefix} failed on {pam_host} -> {target_ip}: {str(e)}", 'error')
                    update_file_size(war_prefix, war_name, status='error')
                try:
                    ssh.close()
                except:
                    pass
                return False

        # Execute distributed uploads
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            items = list(enumerate(selected_wars))
            futures = {
                executor.submit(upload_single_war, item): item[1] 
                for item in items
                if not deployment_state.get('cancelled')
            }
            
            for future in as_completed(futures):
                if deployment_state.get('cancelled'):
                    log_message("⚠ Deployment cancelled", 'warning')
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
        
        if errors:
            log_message(f"⚠ {len(errors)} upload(s) had errors", 'warning')
        
        log_message("═" * 50, 'info')
        log_message("✓ STEP 2 DISTRIBUTED UPLOAD COMPLETED!", 'success')
        return len(errors) == 0
        
    except Exception as e:
        log_message(f"✗ Step 2 failed: {str(e)}", 'error')
        return False
    

def deploy_step3(config, selected_wars):
    """Step 3: Deploy WAR files from utilities to final deployment folders"""
    try:
        log_message("═" * 50, 'info')
        log_message("STEP 3: Deploy to Final Folders", 'success')
        log_message("═" * 50, 'info')
        
        # Connect to target server
        log_message(f"Connecting to {config.TARGET_SERVER}...", 'info')
        ssh = SSHClient(config.TARGET_SERVER, config.TARGET_USER, config.TARGET_PASSWORD)
        ssh.client = ssh.connect().client
        log_message(f"✓ Connected to target server", 'success')
        
        # Validate remote files exist in utilities folder and match the version
        log_message(f"🔍 Validating remote files for version {config.VERSION}...", 'info')
        missing_files = []
        for war_prefix in selected_wars:
            war_file = f"{war_prefix}-{config.VERSION}.war"
            source_war = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
            stdin, stdout, stderr = ssh.client.exec_command(f"test -f {source_war} && echo 'exists'")
            exists = stdout.read().decode().strip()
            if not exists:
                missing_files.append(war_file)
        
        if missing_files:
            log_message(f"✗ Missing files in utilities folder for version {config.VERSION}!", 'error')
            for missing in missing_files:
                log_message(f"  ✗ Not found: {missing}", 'error')
            log_message("", 'info')
            log_message(f"💡 Run Step 2 first to upload and extract version {config.VERSION}", 'warning')
            ssh.close()
            return False
        
        log_message(f"✓ All {len(selected_wars)} files found in utilities for version {config.VERSION}", 'success')
        
        deployment_state['total_files'] = len(selected_wars)
        deployment_state['completed_files'] = 0
        
        war_map = dict(config.WAR_MAPPINGS)
        
        for idx, war_prefix in enumerate(selected_wars, 1):
            if deployment_state.get('cancelled'):
                log_message("⚠ Deployment cancelled", 'warning')
                break
            
            war_file = f"{war_prefix}-{config.VERSION}.war"
            war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
            
            update_progress((idx - 1) / len(selected_wars) * 100, war_name)
            update_file_size(war_prefix, war_name, status='deploying')
            
            log_message(f"[{idx}/{len(selected_wars)}] {war_name}", 'info')
            
            source_war = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
            
            # Get source size
            source_size = get_remote_file_size(ssh, source_war)
            log_message(f"  📦 Source: {format_size(source_size)}", 'info')
            
            # Deploy to final location
            deploy_folder = war_map.get(war_prefix)
            if deploy_folder:
                target_dir = f"{config.TARGET_DEPLOY_BASE}/{deploy_folder}/{config.VERSION}/War"
                log_message(f"  📁 Creating {deploy_folder}...", 'info')
                ssh.client.exec_command(f"mkdir -p {target_dir}")
                
                log_message(f"  📋 Copying to deployment folder...", 'info')
                ssh.client.exec_command(f"cp {source_war} {target_dir}/")
                
                # Verify final deployed size
                final_war = f"{target_dir}/{war_file}"
                final_size = get_remote_file_size(ssh, final_war)
                update_file_size(war_prefix, war_name, target_size=final_size, status='deployed')
                
                log_message(f"  ✓ Deployed to {deploy_folder} ({format_size(final_size)})", 'success')
            else:
                log_message(f"  ⚠ No deployment mapping found for {war_prefix}", 'warning')
            
            deployment_state['completed_files'] = idx
            update_progress(idx / len(selected_wars) * 100, war_name)
        
        ssh.close()
        
        log_message("═" * 50, 'info')
        log_message("✓ STEP 3 COMPLETED!", 'success')
        log_message("═" * 50, 'success')
        log_message("🚀 DEPLOYMENT COMPLETED!", 'success')
        return True
        
    except Exception as e:
        log_message(f"✗ Step 3 failed: {str(e)}", 'error')
        return False


def run_deployment(config, selected_wars, steps):
    """Run deployment in background thread"""
    deployment_state['running'] = True
    deployment_state['cancelled'] = False
    deployment_state['logs'] = []
    deployment_state['progress'] = 0
    deployment_state['file_sizes'] = {}
    
    try:
        if 1 in steps:
            deployment_state['step'] = 1
            if not deploy_step1(config, selected_wars):
                if not deployment_state.get('cancelled'):
                    raise Exception("Step 1 failed")
        
        if 2 in steps and not deployment_state.get('cancelled'):
            deployment_state['step'] = 2
            if not deploy_step2(config, selected_wars):
                if not deployment_state.get('cancelled'):
                    raise Exception("Step 2 failed")
        
        if 3 in steps and not deployment_state.get('cancelled'):
            deployment_state['step'] = 3
            if not deploy_step3(config, selected_wars):
                if not deployment_state.get('cancelled'):
                    raise Exception("Step 3 failed")
        
        if not deployment_state.get('cancelled'):
            update_progress(100)
        
    except Exception as e:
        log_message(f"Deployment failed: {str(e)}", 'error')
    finally:
        deployment_state['running'] = False
        deployment_state['step'] = None
        broadcast_message('complete', {'cancelled': deployment_state.get('cancelled', False)})


@app.route('/')
def index():
    """Main dashboard"""
    return render_template('index.html')


@app.route('/api/events')
def events():
    """Server-Sent Events endpoint for real-time updates"""
    def generate():
        q = queue.Queue()
        clients.append(q)
        try:
            # Send initial status
            yield f"data: {json.dumps({'type': 'status', 'data': deployment_state})}\n\n"
            
            while True:
                try:
                    message = q.get(timeout=30)
                    yield f"data: {json.dumps(message)}\n\n"
                except queue.Empty:
                    # Send keepalive
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        finally:
            if q in clients:
                clients.remove(q)
    
    return Response(generate(), mimetype='text/event-stream',
                   headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive'})


@app.route('/api/config', methods=['GET'])
def get_config():
    """Get current configuration"""
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        config = load_config_from_json(config_path)
        
        # Get target routes info - use first route for display
        target_routes = getattr(config, 'TARGET_ROUTES', [])
        primary_route = target_routes[0] if target_routes else {'host': config.TARGET_SERVER, 'username': config.TARGET_USER}
        
        return jsonify({
            'version': config.VERSION,
            'source_server': config.SOURCE_SERVER,
            'target_server': primary_route.get('host', config.TARGET_SERVER),
            'target_username': primary_route.get('username', config.TARGET_USER),
            'local_path': config.LOCAL_DOWNLOAD_PATH,
            'war_files': [prefix for prefix, _ in config.WAR_MAPPINGS],
            'total_routes': len(target_routes)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config', methods=['POST'])
def update_config():
    """Update configuration"""
    try:
        data = request.json
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        if 'version' in data:
            config['version'] = data['version']
        
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config/advanced', methods=['GET'])
def get_advanced_config():
    """Get full configuration for advanced settings"""
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        return jsonify(config)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config/advanced', methods=['POST'])
def update_advanced_config():
    """Update full configuration from advanced settings"""
    try:
        data = request.json
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        
        # Read current config
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        # Update all sections
        if 'version' in data:
            config['version'] = data['version']
        
        if 'source_server' in data:
            config['source_server'] = {
                'host': data['source_server'].get('host', config.get('source_server', {}).get('host')),
                'username': data['source_server'].get('username', config.get('source_server', {}).get('username')),
                'port': data['source_server'].get('port', config.get('source_server', {}).get('port', 22)),
                'switch_user': data['source_server'].get('switch_user', config.get('source_server', {}).get('switch_user', ''))
            }
        
        if 'target_server' in data:
            # Handle both old single-server and new routes format
            if 'routes' in data['target_server']:
                config['target_server'] = {
                    'routes': data['target_server']['routes'],
                    'port': data['target_server'].get('port', config.get('target_server', {}).get('port', 22))
                }
            else:
                # Legacy single-server format - keep for backwards compatibility
                config['target_server'] = {
                    'host': data['target_server'].get('host', config.get('target_server', {}).get('host')),
                    'username': data['target_server'].get('username', config.get('target_server', {}).get('username')),
                    'port': data['target_server'].get('port', config.get('target_server', {}).get('port', 22))
                }
        
        if 'local' in data:
            config['local'] = data['local']
        
        if 'paths' in data:
            config['paths'] = data['paths']
        
        if 'war_mappings' in data:
            config['war_mappings'] = data['war_mappings']
        
        # Save updated config
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4)
        
        log_message("✓ Configuration updated successfully", 'success')
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config/reset', methods=['POST'])
def reset_config():
    """Reset configuration to defaults"""
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        
        # Default configuration
        default_config = {
            "version": "3.96.34.245",
            "source_server": {
                "host": "10.246.26.148",
                "username": "a-10266",
                "switch_user": "iflight_user",
                "port": 22
            },
            "local": {
                "download_path": "D:\\RMT_TOOLS\\Deployment_KE\\downloads"
            },
            "target_server": {
                "host": "PAM_NV.ibsplc.aero",
                "username": "a-10266@ibsplc.com%iflightkeprod%10.175.1.247",
                "port": 22
            },
            "paths": {
                "source_base": "/iflightneo/S3_BUILD/NonMS/KE",
                "target_utilities": "/iflightneo/global/Utilities",
                "target_deploy_base": "/iflightneo/global/PROD/ifl_prod_KE_crew/NonMS/Deployments"
            },
            "war_mappings": {
                "iflight-crew-cwp-webapp": "CREW_CWP",
                "iflight-crew-dsm-webapp": "CREW_DSM",
                "iflight-crew-integration-webapp": "CREW_INTEGRATION",
                "iflight-crew-messaging-webapp": "CREW_MSG",
                "iflight-crew-mobility-webapp": "CREW_MOBILITY",
                "iflight-crew-notification-webapp": "CREW_NOTIF",
                "iflight-crew-rules-webapp": "CREW_RULE",
                "iflight-crew-scheduler-webapp": "CREW_SCHED",
                "iflight-crew-stats-webapp": "CREW_STATS",
                "iflight-crew-webapp": "CREW_WEB"
            }
        }
        
        # Save default config
        with open(config_path, 'w') as f:
            json.dump(default_config, f, indent=4)
        
        log_message("✓ Configuration reset to defaults", 'success')
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config/passwords', methods=['POST'])
def update_passwords():
    """Update passwords in .env file"""
    try:
        data = request.json
        source_password = data.get('source_password')
        target_password = data.get('target_password')
        
        if not source_password and not target_password:
            return jsonify({'error': 'No passwords provided'}), 400
        
        env_path = os.path.join(os.path.dirname(__file__), '.env')
        
        # Read existing .env file
        env_lines = []
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                env_lines = f.readlines()
        
        # Update or add password lines
        source_updated = False
        target_updated = False
        new_lines = []
        
        for line in env_lines:
            stripped = line.strip()
            if stripped.startswith('SOURCE_SERVER_PASSWORD=') and source_password:
                new_lines.append(f'SOURCE_SERVER_PASSWORD={source_password}\n')
                source_updated = True
            elif stripped.startswith('TARGET_SERVER_PASSWORD=') and target_password:
                new_lines.append(f'TARGET_SERVER_PASSWORD={target_password}\n')
                target_updated = True
            else:
                new_lines.append(line)
        
        # Add new lines if not found
        if source_password and not source_updated:
            new_lines.append(f'\nSOURCE_SERVER_PASSWORD={source_password}\n')
        if target_password and not target_updated:
            new_lines.append(f'TARGET_SERVER_PASSWORD={target_password}\n')
        
        # Write back to .env file
        with open(env_path, 'w') as f:
            f.writelines(new_lines)
        
        # Reload environment variables
        load_dotenv(override=True)
        
        log_message("✓ Passwords updated securely in .env file", 'success')
        
        return jsonify({'success': True})
    except Exception as e:
        log_message(f"✗ Failed to update passwords: {str(e)}", 'error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/deploy', methods=['POST'])
def start_deployment():
    """Start deployment"""
    if deployment_state['running']:
        return jsonify({'error': 'Deployment already running'}), 400
    
    try:
        data = request.json
        selected_wars = data.get('selected_wars', [])
        steps = data.get('steps', [1, 2])
        version = data.get('version')
        target_server = data.get('target_server')
        target_username = data.get('target_username')
        
        if not selected_wars:
            return jsonify({'error': 'No WAR files selected'}), 400
        
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        config = load_config_from_json(config_path)
        
        if version:
            config.VERSION = version
            config.SOURCE_PATH = f"/iflightneo/S3_BUILD/NonMS/KE/{version}/"
            config.TARGET_EXTRACT_PATH = f"/iflightneo/global/Utilities/{version}/Wars"
            log_message(f"✓ Using version from UI: {version}", 'success')
        else:
            log_message(f"✓ Using default version: {config.VERSION}", 'info')
        
        # Update target server details if provided
        if target_server:
            config.TARGET_SERVER = target_server
            log_message(f"✓ Target server: {target_server}", 'info')
        if target_username:
            config.TARGET_USER = target_username
            log_message(f"✓ Target username: {target_username}", 'info')
        
        # Start deployment in background thread
        thread = threading.Thread(
            target=run_deployment,
            args=(config, selected_wars, steps)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/status', methods=['GET'])
def get_status():
    """Get deployment status"""
    return jsonify({
        'running': deployment_state['running'],
        'step': deployment_state['step'],
        'progress': deployment_state['progress'],
        'current_file': deployment_state['current_file'],
        'completed': deployment_state['completed_files'],
        'total': deployment_state['total_files'],
        'file_sizes': deployment_state['file_sizes']
    })


@app.route('/api/test-connection', methods=['POST'])
def test_connection():
    """Test server connections with detailed path verification and dry run"""
    results = {'source': None, 'target': None, 'success': True}
    
    try:
        data = request.json or {}
        version = data.get('version')
        target_server = data.get('target_server')
        target_username = data.get('target_username')
        
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        config = load_config_from_json(config_path)
        
        # Use version from UI if provided
        if version:
            config.VERSION = version
            config.SOURCE_PATH = f"/iflightneo/S3_BUILD/NonMS/KE/{version}/"
            config.TARGET_EXTRACT_PATH = f"/iflightneo/global/Utilities/{version}/Wars"
            log_message(f"🔍 Testing version: {version}", 'info')
        else:
            log_message(f"🔍 Testing default version: {config.VERSION}", 'info')
        
        # Update target server details if provided
        if target_server:
            config.TARGET_SERVER = target_server
        if target_username:
            config.TARGET_USER = target_username
        
        log_message("═" * 60, 'info')
        log_message("🔧 CONNECTIVITY & PATH VERIFICATION TEST", 'info')
        log_message("═" * 60, 'info')
        
        # Test source server
        log_message(f"📡 Testing source server: {config.SOURCE_SERVER}:22", 'info')
        try:
            ssh_source = SSHClient(config.SOURCE_SERVER, config.SOURCE_USER, config.SOURCE_PASSWORD)
            ssh_source.connect()
            
            log_message(f"✓ Source server connected successfully", 'success')
            
            # Verify source paths and list WAR files
            log_message("📂 Verifying source paths and WAR files:", 'info')
            source_wars_dir = f"{config.SOURCE_PATH}Wars"
            
            # Check if Wars directory exists and list files
            output, error, exit_code = ssh_source.execute_command(f"ls -la {source_wars_dir}/ 2>/dev/null")
            if exit_code == 0:
                log_message(f"✓ Source directory exists: {source_wars_dir}", 'success')
                
                # List WAR files for current version
                war_output, war_error, war_exit = ssh_source.execute_command(
                    f"ls -lh {source_wars_dir}/*{config.VERSION}.war 2>/dev/null || echo 'No WAR files found'"
                )
                if war_exit == 0 and 'No WAR files found' not in war_output:
                    log_message("📦 Available WAR files:", 'success')
                    for line in war_output.strip().split('\n'):
                        if '.war' in line:
                            parts = line.split()
                            if len(parts) >= 9:
                                size = parts[4]
                                filename = parts[-1].split('/')[-1]
                                log_message(f"  • {filename} ({size})", 'info')
                else:
                    log_message(f"⚠ No WAR files found for version {config.VERSION}", 'warning')
                    log_message(f"  Directory: {source_wars_dir}", 'info')
            else:
                log_message(f"✗ Source directory not found: {source_wars_dir}", 'error')
                results['success'] = False
            
            # Test /tmp write permissions
            test_file = f"/tmp/deployment_test_{config.VERSION}.tmp"
            output, error, exit_code = ssh_source.execute_command(f"touch {test_file} && rm -f {test_file}")
            if exit_code == 0:
                log_message("✓ /tmp directory writable", 'success')
            else:
                log_message("✗ /tmp directory not writable", 'error')
                results['success'] = False
            
            ssh_source.close()
            results['source'] = 'success'
            
        except Exception as e:
            log_message(f"✗ Source server failed: {str(e)}", 'error')
            results['source'] = str(e)
            results['success'] = False
        
        log_message("─" * 60, 'info')
        
        # Test target server
        log_message(f"📡 Testing target server: {config.TARGET_SERVER}:22", 'info')
        try:
            ssh_target = SSHClient(config.TARGET_SERVER, config.TARGET_USER, config.TARGET_PASSWORD)
            ssh_target.connect()
            
            log_message(f"✓ Target server connected successfully", 'success')
            
            # Verify target paths
            log_message("📂 Verifying target paths:", 'info')
            
            # Check utilities directory
            output, error, exit_code = ssh_target.execute_command(f"ls -la {config.TARGET_EXTRACT_PATH} 2>/dev/null || echo 'Directory does not exist'")
            if 'Directory does not exist' in output:
                log_message(f"📁 Will create: {config.TARGET_EXTRACT_PATH}", 'info')
            else:
                log_message(f"✓ Target utilities path exists: {config.TARGET_EXTRACT_PATH}", 'success')
            
            # Check deployment base directory
            output, error, exit_code = ssh_target.execute_command(f"ls -la {config.TARGET_DEPLOY_BASE} 2>/dev/null")
            if exit_code == 0:
                log_message(f"✓ Deployment base exists: {config.TARGET_DEPLOY_BASE}", 'success')
                
                # List existing deployment folders with all versions (optimized)
                folder_output, folder_error, folder_exit = ssh_target.execute_command(
                    f"ls -1 {config.TARGET_DEPLOY_BASE}/ | grep CREW"
                )
                if folder_exit == 0 and folder_output.strip():
                    log_message("📁 Existing CREW deployment folders:", 'info')
                    
                    folders = [f.strip() for f in folder_output.strip().split('\n') if f.strip()]
                    
                    # Process folders in smaller batches for better performance
                    for folder in folders:
                        full_path = f"{config.TARGET_DEPLOY_BASE}/{folder}"
                        log_message(f"  • {full_path}", 'info')
                        
                        # Get all versions for this folder (no limit)
                        version_output, version_error, version_exit = ssh_target.execute_command(
                            f"ls -1 {full_path}/ 2>/dev/null | sort -V"
                        )
                        if version_exit == 0 and version_output.strip():
                            versions = [v.strip() for v in version_output.strip().split('\n') if v.strip()]
                            if versions:
                                # Check if current deployment version exists
                                current_version = config.VERSION
                                if current_version in versions:
                                    version_status = f"✓ Current version {current_version} exists"
                                    log_message(f"    └─ All Versions ({len(versions)}): {', '.join(versions)}", 'info')
                                    log_message(f"    └─ {version_status}", 'success')
                                else:
                                    version_status = f"📝 Will create version {current_version}"
                                    log_message(f"    └─ All Versions ({len(versions)}): {', '.join(versions)}", 'info')
                                    log_message(f"    └─ {version_status}", 'warning')
                        else:
                            log_message(f"    └─ No versions found", 'warning')
                            log_message(f"    └─ 📝 Will create version {config.VERSION} (first deployment)", 'warning')
                
                # Check existing WAR files for current version
                log_message("─" * 60, 'info')
                log_message(f"📦 Checking existing WAR files for version {config.VERSION}:", 'info')
                
                # Clear previous file sizes for test connection display
                deployment_state['file_sizes'] = {}
                
                war_map = dict(config.WAR_MAPPINGS)
                existing_wars = []
                missing_wars = []
                
                for war_prefix, folder_name in war_map.items():
                    war_file = f"{war_prefix}-{config.VERSION}.war"
                    war_path = f"{config.TARGET_DEPLOY_BASE}/{folder_name}/{config.VERSION}/War/{war_file}"
                    
                    # Extract short name for display (e.g., CREW_CWP from iflight-crew-cwp-webapp)
                    display_name = folder_name.replace('Deploy_', '').upper()
                    
                    # Check if WAR exists and get its size
                    size_output, size_error, size_exit = ssh_target.execute_command(
                        f"if [ -f '{war_path}' ]; then stat -c%s '{war_path}'; else echo 'NOT_FOUND'; fi"
                    )
                    
                    if size_exit == 0 and 'NOT_FOUND' not in size_output:
                        size_bytes = int(size_output.strip())
                        size_human = format_size_human(size_bytes)
                        existing_wars.append((folder_name, war_file, size_human, size_bytes))
                        log_message(f"  ✓ {display_name}: {war_file} ({size_human})", 'success')
                        
                        # Update file sizes table with existing deployed files
                        update_file_size(war_prefix, display_name, target_size=size_bytes, status='deployed')
                    else:
                        missing_wars.append((folder_name, war_file, display_name, war_prefix))
                        
                        # Show missing files as pending in the table
                        update_file_size(war_prefix, display_name, status='pending')
                
                # Summary
                log_message("─" * 60, 'info')
                if existing_wars:
                    log_message(f"📊 Summary: {len(existing_wars)} WAR(s) already deployed, {len(missing_wars)} new", 'info')
                    log_message("  Existing WAR files:", 'success')
                    for folder, war, size, _ in existing_wars:
                        log_message(f"    • {war} - {size}", 'success')
                else:
                    log_message(f"📊 Summary: {len(missing_wars)} WAR(s) to be deployed (fresh deployment)", 'info')
                
                if missing_wars:
                    log_message("  New deployments:", 'warning')
                    for folder, war, display_name, _ in missing_wars:
                        log_message(f"    • {war} - will be created", 'warning')
                
            else:
                log_message(f"✗ Deployment base not found: {config.TARGET_DEPLOY_BASE}", 'error')
                results['success'] = False
            
            # Test /tmp write permissions
            test_file = f"/tmp/deployment_test_{config.VERSION}.tmp"
            output, error, exit_code = ssh_target.execute_command(f"touch {test_file} && rm -f {test_file}")
            if exit_code == 0:
                log_message("✓ /tmp directory writable", 'success')
            else:
                log_message("✗ /tmp directory not writable", 'error')
                results['success'] = False
            
            ssh_target.close()
            results['target'] = 'success'
            
        except Exception as e:
            error_msg = str(e)
            log_message(f"✗ Target server failed: {error_msg}", 'error')
            
            # Provide troubleshooting suggestions
            if 'WinError 10060' in error_msg or 'timed out' in error_msg.lower():
                log_message("💡 Troubleshooting Network Issues:", 'warning')
                log_message("  • Check if you're connected to company VPN", 'warning')
                log_message("  • Verify target server 10.175.1.247 is online", 'warning')
                log_message("  • Check firewall/network access rules", 'warning')
                log_message("  • Contact network admin if needed", 'warning')
                log_message("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", 'info')
                log_message("ℹ Note: You can still try deployment - source server may have access", 'info')
            
            results['target'] = error_msg
            results['success'] = False
        
        # Show deployment dry run commands
        log_message("═" * 60, 'info')
        log_message("🔄 DEPLOYMENT DRY RUN - Commands Preview", 'info')
        log_message("═" * 60, 'info')
        
        log_message("📋 STEP 1 Commands (per WAR file):", 'info')
        sample_war = f"iflight-crew-cwp-webapp-{config.VERSION}.war"
        sample_tar = f"iflight-crew-cwp-webapp-{config.VERSION}.tar"
        
        log_message(f"1. cd {config.SOURCE_PATH}Wars", 'info')
        log_message(f"2. tar -cf /tmp/{sample_tar} {sample_war}", 'info')
        log_message(f"3. scp /tmp/{sample_tar} local:{config.LOCAL_DOWNLOAD_PATH}/", 'info')
        log_message(f"4. md5sum /tmp/{sample_tar}", 'info')
        log_message(f"5. rm -f /tmp/{sample_tar}", 'info')
        
        log_message("─" * 40, 'info')
        log_message("📋 STEP 2 Commands (per WAR file):", 'info')
        
        log_message(f"1. scp {config.LOCAL_DOWNLOAD_PATH}/{sample_tar} target:/tmp/", 'info')
        log_message(f"2. md5sum /tmp/{sample_tar}", 'info')
        log_message(f"3. cd {config.TARGET_EXTRACT_PATH} && tar -xf /tmp/{sample_tar}", 'info')
        log_message(f"4. mkdir -p {config.TARGET_DEPLOY_BASE}/CREW_CWP/{config.VERSION}/War", 'info')
        log_message(f"5. cp {config.TARGET_EXTRACT_PATH}/{sample_war} {config.TARGET_DEPLOY_BASE}/CREW_CWP/{config.VERSION}/War/", 'info')
        log_message(f"6. rm -f /tmp/{sample_tar}", 'info')
        
        log_message("─" * 40, 'info')
        log_message("📊 Deployment Summary:", 'info')
        log_message(f"  • Version: {config.VERSION}", 'info')
        log_message(f"  • WAR files to deploy: {len(config.WAR_MAPPINGS)}", 'info')
        log_message(f"  • Source → Local → Target transfer", 'info')
        log_message(f"  • Individual compression per file", 'info')
        log_message(f"  • MD5 verification at each step", 'info')
        
        if results['success']:
            log_message("✅ All systems ready for deployment!", 'success')
        else:
            log_message("⚠️ Some issues found - check logs above", 'warning')
        
        log_message("═" * 60, 'info')
        
        return jsonify(results)
        
    except Exception as e:
        log_message(f"✗ Configuration error: {str(e)}", 'error')
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/cancel', methods=['POST'])
def cancel_deployment():
    """Cancel running deployment"""
    if deployment_state['running']:
        deployment_state['cancelled'] = True
        log_message("⚠ Cancellation requested...", 'warning')
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'No deployment running'})


if __name__ == '__main__':
    print("=" * 60)
    print("iFlight Neo Wars Deployment - Web Interface")
    print("=" * 60)
    print(f"\n🌐 Open in browser: http://localhost:5000\n")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False, threaded=True)
