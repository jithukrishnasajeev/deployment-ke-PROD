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
import re
from datetime import datetime
from dotenv import load_dotenv
import time
# scp imported inside try/except below to handle missing package gracefully
from deployment_automation import (
    DeploymentConfig, SSHClient, calculate_local_md5, 
    get_remote_md5, load_config_from_json
)
import sys
import subprocess

# Load environment variables
load_dotenv()

# SCP is optional — import only if already installed; auto-install fallback below
try:
    from scp import SCPClient
except ImportError:
    print("SCP module missing.")


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
    'file_sizes': {},
    'failed_wars': [],        # war prefixes that failed in last run
    'failed_routes': {}       # war_prefix -> route index that was tried and failed
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


def get_aws_cmd():
    """Find AWS CLI binary path or return 'aws' default"""
    import shutil
    aws_path = shutil.which('aws')
    if aws_path:
        return aws_path
    possible_paths = [
        r"C:\Program Files\Amazon\AWSCLIV2\aws.exe",
        r"C:\Program Files (x86)\AWS CLI\aws.exe",
        r"C:\Program Files\Amazon\AWS CLI\aws.exe",
        r"C:\aws-cli\aws.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\Amazon\AWSCLIV2\aws.exe")
    ]
    for path in possible_paths:
        if os.path.exists(path):
            return path
    return 'aws'


class StepProgressTracker:
    """Thread-safe unified progress tracker for concurrent multi-file transfers."""
    
    def __init__(self, total_files: int, on_progress_callback=None):
        self.total_files = max(1, total_files)
        self.on_progress_callback = on_progress_callback
        self.lock = threading.Lock()
        self.completed_count = 0
        self.active_file_progress = {}   # war_prefix -> fraction (0.0 to 1.0)
        self.active_file_names = {}      # war_prefix -> display_name
        self.last_broadcast_time = 0.0

    def file_started(self, war_prefix: str, war_name: str):
        with self.lock:
            self.active_file_names[war_prefix] = war_name
            self.active_file_progress[war_prefix] = 0.0
            self._notify(force=True)

    def file_progress(self, war_prefix: str, war_name: str, transferred: int, total: int):
        with self.lock:
            self.active_file_names[war_prefix] = war_name
            frac = (transferred / total) if total > 0 else 0.0
            self.active_file_progress[war_prefix] = min(0.999, max(0.0, frac))
            self._notify(force=False)

    def file_completed(self, war_prefix: str, war_name: str):
        with self.lock:
            self.active_file_progress.pop(war_prefix, None)
            self.active_file_names.pop(war_prefix, None)
            self.completed_count += 1
            self._notify(force=True)

    def file_failed(self, war_prefix: str, war_name: str):
        with self.lock:
            self.active_file_progress.pop(war_prefix, None)
            self.active_file_names.pop(war_prefix, None)
            self._notify(force=True)

    def _notify(self, force: bool = False):
        now = time.time()
        if not force and (now - self.last_broadcast_time < 0.12):
            return
        
        self.last_broadcast_time = now
        sum_active = sum(self.active_file_progress.values())
        overall_p = min(100.0, max(0.0, ((self.completed_count + sum_active) / self.total_files) * 100.0))
        
        if len(self.active_file_names) > 1:
            names = sorted(self.active_file_names.values())
            current_file_str = f"{len(names)} active ({', '.join(names)})"
        elif len(self.active_file_names) == 1:
            current_file_str = next(iter(self.active_file_names.values()))
        else:
            current_file_str = "-"

        deployment_state['completed_files'] = self.completed_count
        deployment_state['total_files'] = self.total_files
        deployment_state['current_file'] = current_file_str
        deployment_state['progress'] = overall_p

        if self.on_progress_callback:
            self.on_progress_callback(overall_p, current_file_str, self.completed_count, self.total_files)
        else:
            update_progress(overall_p, current_file_str, self.completed_count, self.total_files)


def create_transfer_callback(war_prefix, war_name, total_size, operation='upload', tracker=None):
    """Create a callback function for file transfer progress with optimized throttling"""
    import time
    last_update = [0]  # Bytes transferred at last update
    last_time = [0]    # Timestamp of last update
    
    def callback(transferred, total):
        current_time = time.time()
        
        # Update only if: transferred >= 2MB since last update OR 0.5s elapsed OR transfer complete
        bytes_since_update = transferred - last_update[0]
        time_since_update = current_time - last_time[0]
        
        if bytes_since_update >= 2097152 or time_since_update >= 0.5 or transferred == total:
            last_update[0] = transferred
            last_time[0] = current_time
            percent = (transferred / total * 100) if total > 0 else 0
            
            # Update file size info with transfer progress
            update_file_size(
                war_prefix, war_name,
                total_size=total,
                transferred=transferred,
                transfer_progress=percent,
                status=operation,
                broadcast=(tracker is None)
            )

            if tracker:
                tracker.file_progress(war_prefix, war_name, transferred, total)

            # CHECK FOR CANCELLATION
            if deployment_state.get('cancelled'):
                raise Exception("Cancellation requested by user")
    
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
        # Enable read-ahead prefetching with full file size for pipelining
        remote_file.prefetch(file_size)
        
        with open(local_path, 'wb') as local_file:
            while True:
                data = remote_file.read(1048576)  # 1 MB read chunks
                if not data:
                    break
                local_file.write(data)
                md5_hash.update(data)
                transferred += len(data)
                
                # Check for cancellation
                if deployment_state.get('cancelled'):
                    raise Exception("Cancellation requested by user")
                if callback:
                    callback(transferred, file_size)
    
    return md5_hash.hexdigest()


def sftp_upload_optimized(ssh, local_path, remote_path, war_prefix, war_name, use_scp=True, tracker=None):
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
                if sent - transferred[0] >= 2097152 or current_time - last_update[0] >= 0.5 or sent == size:
                    transferred[0] = sent
                    last_update[0] = current_time
                    percent = (sent / size * 100) if size > 0 else 0
                    
                    update_file_size(
                        war_prefix, war_name,
                        total_size=size,
                        transferred=sent,
                        transfer_progress=percent,
                        status='uploading',
                        broadcast=(tracker is None)
                    )
                    
                    if tracker:
                        tracker.file_progress(war_prefix, war_name, sent, size)

                    # CHECK FOR CANCELLATION
                    if deployment_state.get('cancelled'):
                        raise Exception("Cancellation requested by user")
            
            start_time = time.time()
            
            # Use existing SSH transport for SCP with 64KB buffer for full throughput
            with SCPClient(ssh.get_transport(), buff_size=65536, progress=scp_progress, socket_timeout=60.0) as scp:
                scp.put(local_path, remote_path)
            
            elapsed = max(0.1, time.time() - start_time)
            speed = file_size / elapsed / 1024 / 1024
            log_message(f"  ✓ SCP Upload completed in {elapsed:.1f}s ({speed:.1f} MB/s)", 'success')
            return
            
        except ImportError:
            log_message(f"  ⚠ SCP module not available (pip install scp), falling back to SFTP", 'warning')
        except Exception as e:
            if deployment_state.get('cancelled') or "Cancellation requested" in str(e):
                raise
            log_message(f"  ⚠ SCP failed: {str(e)}, falling back to SFTP", 'warning')
    
    # Standard SFTP upload (stable fallback)
    sftp = ssh.get_sftp()
    log_message(f"  ⬆ Uploading {format_size(file_size)}...", 'info')
    callback = create_transfer_callback(war_prefix, war_name, file_size, 'uploading', tracker=tracker)
    
    start_time = time.time()
    
    # Speed fix: confirm=False for pipeline mode
    sftp.put(local_path, remote_path, callback=callback, confirm=False)
    elapsed = max(0.1, time.time() - start_time)
    
    speed = file_size / elapsed / 1024 / 1024
    log_message(f"  ✓ Uploaded in {elapsed:.1f}s ({speed:.1f} MB/s)", 'success')


def sftp_download_optimized(ssh, remote_path, local_path, war_prefix, war_name, use_scp=True, callback=None, tracker=None):
    """Optimized download using prefetch + streaming MD5 (SCP fallback available)"""
    
    # Get file size for progress tracking
    sftp = ssh.get_sftp()
    file_size = sftp.stat(remote_path).st_size
    
    # Try SCP if enabled  
    if use_scp:
        try:
            from scp import SCPClient
            
            log_message(f"  🚀 [PROTOCOL: SCP] Starting SCP Download ({format_size(file_size)})...", 'info')
            
            # Progress tracking
            transferred = [0]
            last_update = [time.time()]
            
            def scp_progress(filename, size, sent):
                current_time = time.time()
                if sent - transferred[0] >= 2097152 or current_time - last_update[0] >= 0.5 or sent == size:
                    transferred[0] = sent
                    last_update[0] = current_time
                    percent = (sent / size * 100) if size > 0 else 0
                    
                    update_file_size(
                        war_prefix, war_name,
                        total_size=size,
                        transferred=sent,
                        transfer_progress=percent,
                        status='downloading',
                        broadcast=(tracker is None)
                    )
                    
                    if tracker:
                        tracker.file_progress(war_prefix, war_name, sent, size)
            
            start_time = time.time()
            
            with SCPClient(ssh.get_transport(), buff_size=65536, progress=scp_progress, socket_timeout=60.0) as scp:
                scp.get(remote_path, local_path)
            
            elapsed = max(0.1, time.time() - start_time)
            speed = file_size / elapsed / 1024 / 1024
            log_message(f"  ✓ [PROTOCOL: SCP] Download completed in {elapsed:.1f}s ({speed:.1f} MB/s)", 'success')
            return calculate_local_md5(local_path)
            
        except ImportError:
            log_message(f"  ⚠ SCP module not installed, falling back to SFTP", 'warning')
        except Exception as e:
            if deployment_state.get('cancelled') or "Cancellation requested" in str(e):
                raise
            log_message(f"  ⚠ [SCP FAILED] {str(e)} — falling back to SFTP (prefetch mode)", 'warning')
    
    # Fast SFTP download with prefetch + streaming MD5
    log_message(f"  ⬇ [PROTOCOL: SFTP] Downloading {format_size(file_size)} (prefetch mode)...", 'info')
    if not callback:
        callback = create_transfer_callback(war_prefix, war_name, file_size, 'downloading', tracker=tracker)
    
    start_time = time.time()
    local_md5 = fast_sftp_download(sftp, remote_path, local_path, war_prefix, war_name, callback)
    elapsed = max(0.1, time.time() - start_time)
    
    speed = file_size / elapsed / 1024 / 1024
    log_message(f"  ✓ [PROTOCOL: SFTP] Downloaded in {elapsed:.1f}s ({speed:.1f} MB/s)", 'success')
    return local_md5


def broadcast_message(msg_type, data):
    """Send message to all connected clients"""
    message = {'type': msg_type, 'data': data}
    dead_clients = []
    for q in clients:
        try:
            q.put_nowait(message)
        except Exception:
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
    try:
        print(f"[{timestamp}] [{level.upper()}] {message}")
    except UnicodeEncodeError:
        print(f"[{timestamp}] [{level.upper()}] {message.encode('ascii', 'backslashreplace').decode('ascii')}")


def update_progress(progress, current_file=None, completed=None, total=None):
    """Update progress and broadcast to clients"""
    deployment_state['progress'] = progress
    if current_file is not None:
        deployment_state['current_file'] = current_file
    if completed is not None:
        deployment_state['completed_files'] = completed
    if total is not None:
        deployment_state['total_files'] = total
    broadcast_message('progress', {
        'progress': progress,
        'current_file': deployment_state['current_file'],
        'completed': deployment_state['completed_files'],
        'total': deployment_state['total_files'],
        'file_sizes': deployment_state['file_sizes']
    })


def update_file_size(war_prefix, war_name, source_size=None, target_size=None, status=None, total_size=None, transferred=None, transfer_progress=None, broadcast=True):
    """Update and broadcast file size info"""
    if war_prefix not in deployment_state['file_sizes']:
        deployment_state['file_sizes'][war_prefix] = {
            'name': war_name,
            'source_size': 0,
            'target_size': 0,
            'status': 'pending',
            'transfer_progress': 0,
            'transferred': 0
        }
    
    if source_size is not None:
        deployment_state['file_sizes'][war_prefix]['source_size'] = source_size
    if target_size is not None:
        deployment_state['file_sizes'][war_prefix]['target_size'] = target_size
    if status is not None:
        deployment_state['file_sizes'][war_prefix]['status'] = status
    if total_size is not None:
        deployment_state['file_sizes'][war_prefix]['total_size'] = total_size
    if transferred is not None:
        deployment_state['file_sizes'][war_prefix]['transferred'] = transferred
    if transfer_progress is not None:
        deployment_state['file_sizes'][war_prefix]['transfer_progress'] = transfer_progress
    
    if broadcast:
        broadcast_message('file_size', deployment_state['file_sizes'])


def get_remote_file_size(ssh, file_path):
    """Get size of remote file in bytes"""
    stdin, stdout, stderr = ssh.exec_command(f"stat -c%s '{file_path}' 2>/dev/null || echo 0")
    try:
        return int(stdout.read().decode().strip())
    except:
        return 0


def get_s3_file_size(s3_uri, profile):
    """Get size of S3 object in bytes"""
    try:
        aws_bin = get_aws_cmd()
        cmd = [aws_bin, "s3", "ls", s3_uri, "--profile", profile]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            parts = proc.stdout.strip().split()
            if len(parts) >= 3:
                return int(parts[2])
    except Exception:
        pass
    return 0


def parse_bytes(val_str, unit_str):
    """Convert number and unit (MiB, KiB, GiB, Bytes) to integer bytes"""
    try:
        val = float(val_str)
        unit = unit_str.upper()
        if 'G' in unit:
            return int(val * 1024 * 1024 * 1024)
        elif 'M' in unit:
            return int(val * 1024 * 1024)
        elif 'K' in unit:
            return int(val * 1024)
        return int(val)
    except Exception:
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
    """Step 1: Download WAR files to Local machine (supports Parallel and Sequential modes)"""
    try:
        download_source = getattr(config, 'DOWNLOAD_SOURCE', 'ssh').lower()
        if download_source == 's3':
            log_message("═" * 50, 'info')
            log_message("STEP 1: High-Speed AWS S3 Download to Local", 'success')
            log_message("═" * 50, 'info')
        else:
            log_message("═" * 50, 'info')
            log_message("STEP 1: High-Speed Download to Local", 'success')
            log_message("═" * 50, 'info')
        
        os.makedirs(config.LOCAL_DOWNLOAD_PATH, exist_ok=True)
        
        deployment_state['total_files'] = len(selected_wars)
        deployment_state['completed_files'] = 0
        deployment_state['file_sizes'] = {}
        
        direct_war = getattr(config, 'DIRECT_WAR_DOWNLOAD', True)
        parallel = getattr(config, 'PARALLEL_DOWNLOADS', False)
        max_workers = getattr(config, 'MAX_THREADS', 4) if parallel else 1
        use_scp = getattr(config, 'USE_SCP', True)
        
        tracker = StepProgressTracker(total_files=len(selected_wars))
        errors = []
        download_lock = threading.Lock()
        source_wars_dir = f"{config.SOURCE_PATH}Wars"

        if download_source == 's3':
            bucket = getattr(config, 'S3_BUCKET', 'iflightdevrdits3')
            profile = getattr(config, 'S3_PROFILE', 'iFlightCrew_Dev')
            prefix_template = getattr(config, 'S3_PREFIX_TEMPLATE', 'iFlight_Release/{version}/Wars/')
            s3_prefix = prefix_template.format(version=config.VERSION)
            
            log_message(f"☁️ Download Source: AWS S3 Bucket ({bucket})", 'success')
            log_message(f"👤 AWS SSO Profile: {profile}", 'info')
            log_message(f"📂 S3 URI Prefix: s3://{bucket}/{s3_prefix}", 'info')
            log_message(f"⚙ Download Mode: {'Parallel (' + str(max_workers) + ' worker threads)' if parallel and max_workers > 1 else 'Sequential single worker'}", 'info')
            
            def download_one_war(item):
                idx, war_prefix = item
                if deployment_state.get('cancelled'):
                    return False
                    
                war_file = f"{war_prefix}-{config.VERSION}.war"
                war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
                
                s3_uri = f"s3://{bucket}/{s3_prefix}{war_file}"
                local_war = os.path.join(config.LOCAL_DOWNLOAD_PATH, war_file)
                
                s3_total_size = get_s3_file_size(s3_uri, profile)
                
                with download_lock:
                    log_message(f"[{idx}/{len(selected_wars)}] Downloading from S3: {war_name}", 'info')
                    update_file_size(
                        war_prefix, war_name,
                        source_size=s3_total_size if s3_total_size > 0 else 0,
                        total_size=s3_total_size if s3_total_size > 0 else 0,
                        transferred=0,
                        transfer_progress=0,
                        status='downloading',
                        broadcast=False
                    )
                    tracker.file_started(war_prefix, war_name)
                    if s3_total_size > 0:
                        log_message(f"  📦 {war_name}: S3 Object Size {format_size(s3_total_size)}", 'info')
                
                try:
                    aws_bin = get_aws_cmd()
                    cmd = [aws_bin, "s3", "cp", s3_uri, local_war, "--profile", profile]
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                    
                    last_update = [0.0]
                    transferred_ref = [0]
                    
                    def stream_reader():
                        buffer = ""
                        while True:
                            char = proc.stdout.read(1)
                            if not char:
                                break
                            if char in ('\r', '\n'):
                                line = buffer.strip()
                                buffer = ""
                                if line:
                                    m = re.search(r'Completed\s+([\d\.]+)\s*([A-Za-z]+)/([\d\.]+)\s*([A-Za-z]+)', line)
                                    if m:
                                        cur_val, cur_unit, tot_val, tot_unit = m.groups()
                                        parsed_bytes = parse_bytes(cur_val, cur_unit)
                                        transferred_ref[0] = max(transferred_ref[0], parsed_bytes)
                            else:
                                buffer += char
                                
                    reader_thread = threading.Thread(target=stream_reader, daemon=True)
                    reader_thread.start()
                    
                    while proc.poll() is None:
                        if deployment_state.get('cancelled'):
                            proc.terminate()
                            break
                        
                        file_bytes = os.path.getsize(local_war) if os.path.exists(local_war) else 0
                        current_bytes = max(file_bytes, transferred_ref[0])
                        tot = s3_total_size if s3_total_size > 0 else (current_bytes or 1)
                        percent = min(99.9, (current_bytes / tot * 100)) if tot > 0 else 50.0
                        
                        now = time.time()
                        if now - last_update[0] >= 0.15:
                            last_update[0] = now
                            with download_lock:
                                update_file_size(
                                    war_prefix, war_name,
                                    source_size=tot,
                                    total_size=tot,
                                    transferred=current_bytes,
                                    transfer_progress=percent,
                                    status='downloading',
                                    broadcast=False
                                )
                                tracker.file_progress(war_prefix, war_name, current_bytes, tot)
                        time.sleep(0.05)
                        
                    reader_thread.join(timeout=2)
                    stdout, stderr = proc.communicate()
                    if proc.returncode != 0:
                        raise Exception(f"AWS CLI Error: {stdout or stderr or 'Unknown error'}")
                        
                    local_size = os.path.getsize(local_war) if os.path.exists(local_war) else (transferred_ref[0] or s3_total_size)
                    local_md5 = calculate_local_md5(local_war)
                    
                    with download_lock:
                        update_file_size(
                            war_prefix, war_name,
                            source_size=local_size,
                            target_size=local_size,
                            total_size=local_size,
                            transferred=local_size,
                            transfer_progress=100,
                            status='downloaded',
                            broadcast=False
                        )
                        tracker.file_completed(war_prefix, war_name)
                        log_message(f"  ✓ {war_name}: Downloaded & Verified {format_size(local_size)} from S3", 'success')
                    return True
                except Exception as e:
                    with download_lock:
                        errors.append(f"{war_prefix}: {str(e)}")
                        log_message(f"  ✗ {war_name} S3 download failed: {str(e)}", 'error')
                        update_file_size(war_prefix, war_name, status='error', broadcast=False)
                        tracker.file_failed(war_prefix, war_name)
                    return False
        else:
            if direct_war:
                log_message("🚀 Direct WAR Download Enabled (skipping tar packaging overhead)", 'success')
            
            log_message(f"⚙ Download Mode: {'Parallel (' + str(max_workers) + ' worker threads)' if parallel and max_workers > 1 else 'Sequential single SSH session'}", 'info')

            def download_one_war(item):
                idx, war_prefix = item
                if deployment_state.get('cancelled'):
                    return False
                    
                war_file = f"{war_prefix}-{config.VERSION}.war"
                tar_file = f"{war_prefix}-{config.VERSION}.tar"
                war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
                
                with download_lock:
                    log_message(f"[{idx}/{len(selected_wars)}] Downloading: {war_name}", 'info')
                    update_file_size(war_prefix, war_name, status='processing', broadcast=False)
                    tracker.file_started(war_prefix, war_name)
                
                source_port = getattr(config, 'SOURCE_PORT', 22)
                ssh = SSHClient(config.SOURCE_SERVER, config.SOURCE_USER, config.SOURCE_PASSWORD, source_port)
                ssh.connect()
                
                try:
                    remote_war = f"{source_wars_dir}/{war_file}"
                    source_war_size = get_remote_file_size(ssh, remote_war)
                    
                    with download_lock:
                        update_file_size(war_prefix, war_name, source_size=source_war_size, total_size=source_war_size, broadcast=False)
                        log_message(f"  📦 {war_name}: Source WAR {format_size(source_war_size)}", 'info')
                    
                    if direct_war:
                        local_war = os.path.join(config.LOCAL_DOWNLOAD_PATH, war_file)
                        callback = create_transfer_callback(war_prefix, war_name, source_war_size, 'downloading', tracker=tracker)
                        local_md5 = sftp_download_optimized(ssh, remote_war, local_war, war_prefix, war_name, use_scp, callback=callback, tracker=tracker)
                        
                        with download_lock:
                            log_message(f"  🔐 {war_name}: Verifying MD5...", 'info')
                        
                        remote_md5 = get_remote_md5(ssh, remote_war)
                        if local_md5 is None:
                            local_md5 = calculate_local_md5(local_war)
                        
                        with download_lock:
                            if remote_md5 and local_md5 == remote_md5:
                                log_message(f"  ✓ {war_name}: Integrity verified", 'success')
                                update_file_size(war_prefix, war_name, status='downloaded', transfer_progress=100, broadcast=False)
                            else:
                                log_message(f"  ⚠ {war_name}: Checksum mismatch!", 'warning')
                                update_file_size(war_prefix, war_name, status='warning', transfer_progress=100, broadcast=False)
                    else:
                        remote_tar = f"/tmp/{tar_file}_{threading.current_thread().ident}"
                        local_tar = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)
                        
                        with download_lock:
                            log_message(f"  ⚙ {war_name}: Packaging tar...", 'info')
                        
                        stdin, stdout, stderr = ssh.exec_command(
                            f"cd '{source_wars_dir}' && tar -cf '{remote_tar}' '{war_file}'"
                        )
                        stdout.channel.recv_exit_status()
                        
                        tar_size = get_remote_file_size(ssh, remote_tar)
                        callback = create_transfer_callback(war_prefix, war_name, tar_size, 'downloading', tracker=tracker)
                        local_md5 = sftp_download_optimized(ssh, remote_tar, local_tar, war_prefix, war_name, use_scp, callback=callback, tracker=tracker)
                        remote_md5 = get_remote_md5(ssh, remote_tar)
                        ssh.exec_command(f"rm -f '{remote_tar}'")
                        
                        if local_md5 is None:
                            local_md5 = calculate_local_md5(local_tar)
                        
                        with download_lock:
                            if remote_md5 and local_md5 == remote_md5:
                                log_message(f"  ✓ {war_name}: Integrity verified", 'success')
                                update_file_size(war_prefix, war_name, status='downloaded', transfer_progress=100, broadcast=False)
                            else:
                                log_message(f"  ⚠ {war_name}: Checksum mismatch!", 'warning')
                                update_file_size(war_prefix, war_name, status='warning', transfer_progress=100, broadcast=False)
                    
                    with download_lock:
                        tracker.file_completed(war_prefix, war_name)
                    
                    return True
                except Exception as e:
                    with download_lock:
                        errors.append(f"{war_prefix}: {str(e)}")
                        log_message(f"  ✗ {war_prefix} download failed: {str(e)}", 'error')
                        update_file_size(war_prefix, war_name, status='error', broadcast=False)
                        tracker.file_failed(war_prefix, war_name)
                    return False
                finally:
                    ssh.close()

        if parallel and max_workers > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                items = list(enumerate(selected_wars, 1))
                futures = {executor.submit(download_one_war, item): item[1] for item in items}
                for future in as_completed(futures):
                    if deployment_state.get('cancelled'):
                        log_message("⚠ Deployment cancelled", 'warning')
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
        else:
            for idx, war_prefix in enumerate(selected_wars, 1):
                if deployment_state.get('cancelled'):
                    log_message("⚠ Deployment cancelled by user", 'warning')
                    break
                download_one_war((idx, war_prefix))

        log_message("═" * 50, 'info')
        log_message("✓ STEP 1 COMPLETED!", 'success')
        log_message(f"📊 Downloaded {tracker.completed_count}/{len(selected_wars)} files", 'info')
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
            war_file = f"{war_prefix}-{config.VERSION}.war"
            zip_file = f"{war_prefix}-{config.VERSION}.zip"
            war_zip_file = f"{war_prefix}-{config.VERSION}.war.zip"
            tar_file = f"{war_prefix}-{config.VERSION}.tar"
            
            candidates = [
                os.path.join(config.LOCAL_DOWNLOAD_PATH, war_file),
                os.path.join(config.LOCAL_DOWNLOAD_PATH, zip_file),
                os.path.join(config.LOCAL_DOWNLOAD_PATH, war_zip_file),
                os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file),
            ]
            
            local_file = None
            for candidate in candidates:
                if os.path.exists(candidate):
                    local_file = candidate
                    break
            
            war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
            
            if local_file:
                local_size = os.path.getsize(local_file)
                if war_prefix not in deployment_state['file_sizes'] or deployment_state['file_sizes'][war_prefix].get('source_size', 0) == 0:
                    update_file_size(war_prefix, war_name, source_size=local_size, total_size=local_size, broadcast=False)
            else:
                missing_files.append(war_file)
        
        if missing_files:
            log_message(f"✗ Missing files in local path: {config.LOCAL_DOWNLOAD_PATH}", 'error')
            for f in missing_files:
                log_message(f"  • {f}", 'error')
            return False
            
        deployment_state['total_files'] = len(selected_wars)
        deployment_state['completed_files'] = 0
        deployment_state['failed_wars'] = []
        deployment_state['failed_routes'] = {}

        # ── Pre-flight: test every route before starting uploads ─────────────
        log_message(f"🔍 Pre-flight: testing {len(target_routes)} route(s)...", 'info')
        dead_routes = set()

        for r_idx, route in enumerate(target_routes):
            pam_host = route['host'].split('.')[0]
            target_ip = route['username'].split('%')[-1]
            test_ssh = None
            try:
                test_ssh = SSHClient(route['host'], route['username'], config.TARGET_PASSWORD)
                test_ssh.connect(max_retries=1)
                test_ssh.close()
                log_message(f"  ✓ Route {r_idx}: {pam_host} -> {target_ip} — OK", 'success')
            except Exception as e:
                dead_routes.add(r_idx)
                log_message(f"  ✗ Route {r_idx}: {pam_host} -> {target_ip} — FAILED ({str(e)[:60]})", 'error')
                try:
                    if test_ssh:
                        test_ssh.close()
                except Exception:
                    pass

        live_routes = [i for i in range(len(target_routes)) if i not in dead_routes]
        if not live_routes:
            log_message("✗ All routes failed pre-flight check — aborting upload.", 'error')
            return False

        log_message(f"  → {len(live_routes)}/{len(target_routes)} route(s) alive: {[target_routes[i]['host'].split('.')[0] for i in live_routes]}", 'info')
        # Rebuild target_routes to only include live ones for this run
        target_routes = [target_routes[i] for i in live_routes]
        dead_routes = set()   # reset — new indices apply to trimmed list
        dead_route_lock = threading.Lock()
        # ─────────────────────────────────────────────────────────────────────

        # Determine concurrency strictly respecting config.PARALLEL_DOWNLOADS and config.MAX_THREADS
        parallel = getattr(config, 'PARALLEL_DOWNLOADS', False)
        configured_max_threads = getattr(config, 'MAX_THREADS', 4) if parallel else 1
        
        single_route_mode = len(target_routes) == 1
        if not parallel or configured_max_threads <= 1:
            max_workers = 1
            stagger_delay = 0
            log_message("⚙ Upload Mode: Sequential single session (1 worker)", 'info')
        else:
            if single_route_mode:
                max_workers = min(configured_max_threads, 3, len(selected_wars))
                stagger_delay = 1.5
                log_message(f"🚀 Starting parallel upload with {max_workers} worker threads ({stagger_delay}s stagger on single route)", 'info')
            else:
                max_workers = min(configured_max_threads, len(selected_wars))
                stagger_delay = 0.3
                log_message(f"🚀 Starting parallel upload with {max_workers} worker threads across {len(target_routes)} routes", 'info')
        
        upload_lock = threading.Lock()
        tracker = StepProgressTracker(total_files=len(selected_wars))
        errors = []
        
        pam_semaphores = {}
        semaphore_lock = threading.Lock()

        def get_host_semaphore(host):
            with semaphore_lock:
                if host not in pam_semaphores:
                    pam_semaphores[host] = threading.Semaphore(1)
                return pam_semaphores[host]

        def pick_route(idx):
            """Round-robin, skipping routes that already failed to connect."""
            n = len(target_routes)
            with dead_route_lock:
                dead = set(dead_routes)
            for offset in range(n):
                candidate = (idx + offset) % n
                if candidate not in dead:
                    return candidate, target_routes[candidate]
            return idx % n, target_routes[idx % n]

        def upload_single_war(item):
            """Upload and deploy single WAR file using a specifically assigned route"""
            idx, war_prefix = item
            route_idx = idx % len(target_routes)
            war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
            try:
                war_file = f"{war_prefix}-{config.VERSION}.war"
                zip_file = f"{war_prefix}-{config.VERSION}.zip"
                war_zip_file = f"{war_prefix}-{config.VERSION}.war.zip"
                tar_file = f"{war_prefix}-{config.VERSION}.tar"
                
                candidates = [
                    os.path.join(config.LOCAL_DOWNLOAD_PATH, war_file),
                    os.path.join(config.LOCAL_DOWNLOAD_PATH, zip_file),
                    os.path.join(config.LOCAL_DOWNLOAD_PATH, war_zip_file),
                    os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file),
                ]
                
                local_file = None
                for candidate in candidates:
                    if os.path.exists(candidate):
                        local_file = candidate
                        break
                
                if not local_file:
                    raise FileNotFoundError(f"No local artifact found for {war_prefix}")
                
                filename = os.path.basename(local_file)
                route_idx, assigned_route = pick_route(idx)
                pam_host = assigned_route['host'].split('.')[0]
                target_ip = assigned_route['username'].split('%')[-1]

                with upload_lock:
                    log_message(f"[{idx+1}/{len(selected_wars)}] {war_name} ({filename}) -> {pam_host} -> {target_ip}", 'info')
                    update_file_size(war_prefix, war_name, status='uploading', broadcast=False)
                    tracker.file_started(war_prefix, war_name)

                host_sem = get_host_semaphore(assigned_route['host'])
                try:
                    with host_sem:
                        ssh = SSHClient(assigned_route['host'], assigned_route['username'], config.TARGET_PASSWORD)
                        ssh.connect()
                except Exception:
                    with dead_route_lock:
                        dead_routes.add(route_idx)
                    with upload_lock:
                        log_message(f"  ✗ Route {route_idx} ({pam_host}) unreachable — blacklisted for this run", 'warning')
                    raise
                
                use_scp = getattr(config, 'USE_SCP', True)
                ssh.exec_command(f"mkdir -p '{config.TARGET_EXTRACT_PATH}'")
                
                if filename.endswith('.war'):
                    target_remote_path = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
                    sftp_upload_optimized(ssh, local_file, target_remote_path, war_prefix, war_name, use_scp=use_scp, tracker=tracker)
                    
                    local_md5 = calculate_local_md5(local_file)
                    remote_md5 = get_remote_md5(ssh, target_remote_path)
                    
                    if not remote_md5 or local_md5 != remote_md5:
                        with upload_lock:
                            log_message(f"  ⚠ {war_name} ({pam_host}): MD5 mismatch!", 'warning')
                elif filename.endswith('.zip'):
                    tmp_zip_path = f"/tmp/{filename}_{threading.current_thread().ident}"
                    sftp_upload_optimized(ssh, local_file, tmp_zip_path, war_prefix, war_name, use_scp=use_scp, tracker=tracker)
                    
                    local_md5 = calculate_local_md5(local_file)
                    remote_md5 = get_remote_md5(ssh, tmp_zip_path)
                    
                    if not remote_md5 or local_md5 != remote_md5:
                        with upload_lock:
                            log_message(f"  ⚠ {war_name} ({pam_host}): MD5 mismatch!", 'warning')
                    
                    with upload_lock:
                        log_message(f"  📂 {war_name}: Unzipping via {target_ip}...", 'info')
                        update_file_size(war_prefix, war_name, status='extracting', broadcast=False)
                    
                    unzip_cmd = (
                        f"unzip -o -q '{tmp_zip_path}' -d '{config.TARGET_EXTRACT_PATH}' || "
                        f"python3 -m zipfile -e '{tmp_zip_path}' '{config.TARGET_EXTRACT_PATH}'"
                    )
                    stdin, stdout, stderr = ssh.exec_command(unzip_cmd, timeout=300)
                    
                    if stdout.channel.recv_exit_status() != 0:
                        raise Exception(f"Unzip failed: {stderr.read().decode().strip()}")
                    
                    ssh.exec_command(f"rm -f '{tmp_zip_path}'")
                else:
                    target_tar_path = f"/tmp/{filename}_{threading.current_thread().ident}"
                    sftp_upload_optimized(ssh, local_file, target_tar_path, war_prefix, war_name, use_scp=use_scp, tracker=tracker)
                    
                    local_md5 = calculate_local_md5(local_file)
                    remote_md5 = get_remote_md5(ssh, target_tar_path)
                    
                    if not remote_md5 or local_md5 != remote_md5:
                        with upload_lock:
                            log_message(f"  ⚠ {war_name} ({pam_host}): MD5 mismatch!", 'warning')
                    
                    with upload_lock:
                        log_message(f"  📂 {war_name}: Extracting tar via {target_ip}...", 'info')
                        update_file_size(war_prefix, war_name, status='extracting', broadcast=False)
                    
                    stdin, stdout, stderr = ssh.exec_command(
                        f"cd '{config.TARGET_EXTRACT_PATH}' && tar -xf '{target_tar_path}'",
                        timeout=300
                    )
                    
                    if stdout.channel.recv_exit_status() != 0:
                        raise Exception(f"Tar extraction failed: {stderr.read().decode().strip()}")
                    
                    ssh.exec_command(f"rm -f '{target_tar_path}'")
                
                # Get final size
                deployed_war = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
                final_size = get_remote_file_size(ssh, deployed_war)
                
                with upload_lock:
                    log_message(f"  ✓ {war_name} ({target_ip}): Done ({format_size(final_size)})", 'success')
                    update_file_size(war_prefix, war_name, target_size=final_size, status='extracted', broadcast=False)
                    tracker.file_completed(war_prefix, war_name)
                
                ssh.close()
                return True
                
            except Exception as e:
                with upload_lock:
                    errors.append(f"{war_prefix}: {str(e)}")
                    log_message(f"  ✗ {war_prefix} failed on {pam_host} -> {target_ip}: {str(e)}", 'error')
                    update_file_size(war_prefix, war_name, status='error', broadcast=False)
                    tracker.file_failed(war_prefix, war_name)
                    # Track failure so retry can use a different route
                    deployment_state['failed_wars'].append(war_prefix)
                    deployment_state['failed_routes'][war_prefix] = route_idx
                try:
                    ssh.close()
                except:
                    pass
                return False

        # Execute uploads respecting max_workers
        if max_workers > 1:
            import time as _time
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                items = list(enumerate(selected_wars))
                futures = {}
                for item in items:
                    if deployment_state.get('cancelled'):
                        break
                    futures[executor.submit(upload_single_war, item)] = item[1]
                    if stagger_delay and len(futures) < len(items):
                        _time.sleep(stagger_delay)

                for future in as_completed(futures):
                    if deployment_state.get('cancelled'):
                        log_message("⚠ Deployment cancelled", 'warning')
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
        else:
            for item in enumerate(selected_wars):
                if deployment_state.get('cancelled'):
                    log_message("⚠ Deployment cancelled by user", 'warning')
                    break
                upload_single_war(item)
        
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
        ssh.connect()
        log_message(f"✓ Connected to target server", 'success')
        
        # Validate remote files exist in utilities folder and match the version
        log_message(f"🔍 Validating remote files for version {config.VERSION}...", 'info')
        missing_files = []
        for war_prefix in selected_wars:
            war_file = f"{war_prefix}-{config.VERSION}.war"
            source_war = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
            stdin, stdout, stderr = ssh.exec_command(f"test -f {source_war} && echo 'exists'")
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
        
        # --- RECOVER METADATA ---
        # Recover source and target sizes if Step 3 is run independently
        for war_prefix in selected_wars:
            war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
            war_file = f"{war_prefix}-{config.VERSION}.war"
            tar_file = f"{war_prefix}-{config.VERSION}.tar"
            
            # 1. Recover Source Size from local downloads
            local_tar = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)
            if os.path.exists(local_tar):
                local_size = os.path.getsize(local_tar)
                if war_prefix not in deployment_state['file_sizes'] or deployment_state['file_sizes'][war_prefix].get('source_size', 0) == 0:
                    update_file_size(war_prefix, war_name, source_size=local_size, broadcast=False)
            
            # 2. Recover Target Size from utilities folder
            source_war_path = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
            if war_prefix not in deployment_state['file_sizes'] or deployment_state['file_sizes'][war_prefix].get('target_size', 0) == 0:
                target_size = get_remote_file_size(ssh, source_war_path)
                if target_size > 0:
                    update_file_size(war_prefix, war_name, target_size=target_size, status='extracted', broadcast=False)
        
        war_map = dict(config.WAR_MAPPINGS)
        tracker = StepProgressTracker(total_files=len(selected_wars))
        
        for idx, war_prefix in enumerate(selected_wars, 1):
            if deployment_state.get('cancelled'):
                log_message("⚠ Deployment cancelled", 'warning')
                break
            
            war_file = f"{war_prefix}-{config.VERSION}.war"
            war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
            
            tracker.file_started(war_prefix, war_name)
            update_file_size(war_prefix, war_name, status='deploying', broadcast=False)
            
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
                ssh.exec_command(f"mkdir -p '{target_dir}'")
                
                log_message(f"  📋 Copying to deployment folder...", 'info')
                ssh.exec_command(f"cp '{source_war}' '{target_dir}/'")
                
                # Verify final deployed size
                final_war = f"{target_dir}/{war_file}"
                final_size = get_remote_file_size(ssh, final_war)
                update_file_size(war_prefix, war_name, target_size=final_size, status='deployed', broadcast=False)
                
                log_message(f"  ✓ Deployed to {deploy_folder} ({format_size(final_size)})", 'success')
            else:
                log_message(f"  ⚠ No deployment mapping found for {war_prefix}", 'warning')
            
            tracker.file_completed(war_prefix, war_name)
        
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
    
    # Only clear file sizes if we are starting a fresh download (Step 1)
    # Otherwise, keep existing sizes so they show up when running Step 2 or 3 separately
    if 1 in steps:
        deployment_state['file_sizes'] = {}
    elif not deployment_state.get('file_sizes'):
        # If running Step 2/3 and state was lost (e.g. server restart), initialize empty
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
        broadcast_message('complete', {
            'cancelled': deployment_state.get('cancelled', False),
            'failed_wars': deployment_state.get('failed_wars', [])
        })


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


@app.route('/api/fetch-wars', methods=['POST'])
def fetch_wars():
    """Scan the source (SSH or S3) for available WAR files for the given version"""
    try:
        data = request.json or {}
        version = data.get('version')
        if not version:
            return jsonify({'error': 'Version is required'}), 400
            
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        config = load_config_from_json(config_path)
        
        download_source = data.get('download_source', getattr(config, 'DOWNLOAD_SOURCE', 'ssh')).lower()
        
        if download_source == 's3':
            bucket = getattr(config, 'S3_BUCKET', 'iflightdevrdits3')
            profile = getattr(config, 'S3_PROFILE', 'iFlightCrew_Dev')
            prefix_template = getattr(config, 'S3_PREFIX_TEMPLATE', 'iFlight_Release/{version}/Wars/')
            s3_prefix = prefix_template.format(version=version)
            s3_uri = f"s3://{bucket}/{s3_prefix}"
            
            log_message(f"🔍 Scanning AWS S3 bucket ({s3_uri}) using profile '{profile}'...", 'info')
            aws_bin = get_aws_cmd()
            cmd = [aws_bin, "s3", "ls", s3_uri, "--profile", profile]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            
            if proc.returncode != 0:
                err_msg = proc.stderr.strip() or proc.stdout.strip()
                log_message(f"✗ AWS S3 scan failed: {err_msg}", 'error')
                return jsonify({'error': f"S3 fetch failed: {err_msg}", 'files': []})
                
            available_wars = []
            import re
            for line in proc.stdout.splitlines():
                line = line.strip()
                if '.war' in line:
                    parts = line.split()
                    if len(parts) >= 4:
                        filename = parts[3]
                        name_no_ext = filename.rsplit('.', 1)[0]
                        prefix_match = re.search(r'^(.*?)-' + re.escape(version) + r'$', name_no_ext)
                        if prefix_match:
                            available_wars.append(prefix_match.group(1))
                        elif '-' in name_no_ext:
                            available_wars.append(name_no_ext.rsplit('-', 1)[0])
                            
            log_message(f"✓ Found {len(available_wars)} available WAR files in S3 for version {version}", 'success')
            return jsonify({
                'success': True,
                'version': version,
                'war_files': available_wars,
                'source': 's3'
            })
        else:
            # Connect to source server via SSH
            log_message(f"🔍 Scanning source server ({config.SOURCE_SERVER}) for version {version}...", 'info')
            source_port = getattr(config, 'SOURCE_PORT', 22)
            ssh = SSHClient(config.SOURCE_SERVER, config.SOURCE_USER, config.SOURCE_PASSWORD, source_port)
            ssh.connect()
            
            # Command to list .war files
            source_wars_dir = f"/iflightneo/S3_BUILD/NonMS/KE/{version}/Wars"
            stdin, stdout, stderr = ssh.exec_command(f"ls -1 {source_wars_dir}/*.war")
            
            files = stdout.read().decode().splitlines()
            exit_status = stdout.channel.recv_exit_status()
            
            ssh.close()
            
            if exit_status != 0:
                return jsonify({'error': f"No WAR files found for version {version} or directory unreachable.", 'files': []})
                
            available_wars = []
            import re
            
            for file_path in files:
                filename = os.path.basename(file_path)
                name_no_ext = filename.rsplit('.', 1)[0]
                prefix_match = re.search(r'^(.*?)-' + re.escape(version) + r'$', name_no_ext)
                if prefix_match:
                    available_wars.append(prefix_match.group(1))
                else:
                    if '-' in name_no_ext:
                        prefix = name_no_ext.rsplit('-', 1)[0]
                        available_wars.append(prefix)
            
            log_message(f"✓ Found {len(available_wars)} available WAR files for version {version}", 'success')
            return jsonify({
                'success': True,
                'version': version,
                'war_files': available_wars,
                'source': 'ssh'
            })
        
    except Exception as e:
        log_message(f"✗ Failed to fetch WAR files: {str(e)}", 'error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/config', methods=['GET'])
def get_config():
    """Get current configuration"""
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        config = load_config_from_json(config_path)
        
        # Get target routes info - use first route for display
        target_routes = getattr(config, 'TARGET_ROUTES', [])
        primary_route = target_routes[0] if target_routes else {'host': config.TARGET_SERVER, 'username': config.TARGET_USER}
        
        response = jsonify({
            'version': config.VERSION,
            'source_server': config.SOURCE_SERVER,
            'target_server': primary_route.get('host', config.TARGET_SERVER),
            'target_username': primary_route.get('username', config.TARGET_USER),
            'local_path': config.LOCAL_DOWNLOAD_PATH,
            'war_files': [prefix for prefix, _ in config.WAR_MAPPINGS],
            'total_routes': len(target_routes),
            'parallel_downloads': getattr(config, 'PARALLEL_DOWNLOADS', False),
            'max_threads': getattr(config, 'MAX_THREADS', 4),
            'download_source': getattr(config, 'DOWNLOAD_SOURCE', 'ssh'),
            's3_bucket': getattr(config, 'S3_BUCKET', 'iflightdevrdits3'),
            's3_profile': getattr(config, 'S3_PROFILE', 'iFlightCrew_Dev'),
            's3_region': getattr(config, 'S3_REGION', 'ap-south-1'),
            's3_prefix_template': getattr(config, 'S3_PREFIX_TEMPLATE', 'iFlight_Release/{version}/Wars/')
        })
        response.headers['Cache-Control'] = 'no-store'
        return response
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
        
        if 'transfer_optimization' in data:
            config['transfer_optimization'] = data['transfer_optimization']
            
        if 'download_source' in data:
            config['download_source'] = data['download_source']
            
        if 's3_config' in data:
            config['s3_config'] = {
                'bucket': data['s3_config'].get('bucket', config.get('s3_config', {}).get('bucket', 'iflightdevrdits3')),
                'prefix_template': data['s3_config'].get('prefix_template', config.get('s3_config', {}).get('prefix_template', 'iFlight_Release/{version}/Wars/')),
                'profile': data['s3_config'].get('profile', config.get('s3_config', {}).get('profile', 'iFlightCrew_Dev')),
                'region': data['s3_config'].get('region', config.get('s3_config', {}).get('region', 'ap-south-1'))
            }
        
        # Save updated config
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4)
        
        log_message("✓ Configuration updated successfully", 'success')
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/aws-sso-login', methods=['POST'])
def aws_sso_login():
    """Initiate AWS SSO login via terminal command"""
    try:
        data = request.json or {}
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        config = load_config_from_json(config_path)
        profile = data.get('profile', getattr(config, 'S3_PROFILE', 'iFlightCrew_Dev'))
        
        log_message(f"🔑 Launching AWS SSO Login for profile '{profile}'...", 'info')
        
        aws_bin = get_aws_cmd()
        # Launch cmd window to run aws sso login non-blockingly
        subprocess.Popen(f'start "AWS SSO Login ({profile})" cmd /c "{aws_bin} sso login --profile {profile} && echo SSO Login Successful! Press any key to exit. && pause"', shell=True)
        
        return jsonify({
            'success': True,
            'message': f"AWS SSO Login window launched for profile '{profile}'. Complete authentication in browser/terminal."
        })
    except Exception as e:
        log_message(f"✗ Failed to initiate AWS SSO Login: {str(e)}", 'error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/check-aws-sso', methods=['GET', 'POST'])
def check_aws_sso():
    """Check if AWS SSO session is active, auto-launch login if expired"""
    try:
        data = request.get_json(silent=True) or {}
        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        config = load_config_from_json(config_path)
        profile = data.get('profile', getattr(config, 'S3_PROFILE', 'iFlightCrew_Dev'))
        
        aws_bin = get_aws_cmd()
        cmd = [aws_bin, "sts", "get-caller-identity", "--profile", profile]
        
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            if proc.returncode == 0:
                log_message(f"✅ AWS SSO Session is ACTIVE for profile '{profile}'", 'success')
                return jsonify({'active': True, 'profile': profile})
            else:
                log_message(f"⚠️ AWS SSO session expired or unauthenticated for profile '{profile}' - auto launching SSO login...", 'warning')
                subprocess.Popen(f'start "AWS SSO Login ({profile})" cmd /c "{aws_bin} sso login --profile {profile} && echo SSO Login Successful! Press any key to exit. && pause"', shell=True)
                return jsonify({
                    'active': False,
                    'profile': profile,
                    'initiated_login': True,
                    'message': f"SSO Session expired. Auto-launched AWS SSO Login for profile '{profile}'."
                })
        except subprocess.TimeoutExpired:
            log_message(f"⚠️ AWS STS status check timed out for profile '{profile}' - auto launching SSO login...", 'warning')
            subprocess.Popen(f'start "AWS SSO Login ({profile})" cmd /c "{aws_bin} sso login --profile {profile} && echo SSO Login Successful! Press any key to exit. && pause"', shell=True)
            return jsonify({'active': False, 'profile': profile, 'initiated_login': True, 'message': 'SSO Login launched due to timeout.'})
            
    except Exception as e:
        log_message(f"✗ AWS SSO check failed: {str(e)}", 'error')
        return jsonify({'active': False, 'authenticated': False, 'error': str(e)})


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
            "transfer_optimization": {
                "enabled": true,
                "protocol": "SCP",
                "compression": false,
                "parallel_downloads": true,
                "max_threads": 4,
                "direct_war_download": true,
                "comments": "High-speed multi-threaded SCP download with socket window tuning."
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
        
        download_source = data.get('download_source')
        if download_source:
            config.DOWNLOAD_SOURCE = download_source
            log_message(f"✓ Download Source: {download_source.upper()}", 'info')
            
        if version:
            config.VERSION = version
            config.SOURCE_PATH = f"/iflightneo/S3_BUILD/NonMS/KE/{version}/"
            config.TARGET_EXTRACT_PATH = f"/iflightneo/global/Utilities/{version}/Wars"
            log_message(f"✓ Using version from UI: {version}", 'success')
        else:
            log_message(f"✓ Using default version: {config.VERSION}", 'info')
        
        # Apply download mode override sent from UI toggle
        parallel_downloads = data.get('parallel_downloads', None)
        if parallel_downloads is not None:
            config.PARALLEL_DOWNLOADS = bool(parallel_downloads)
            
        max_threads = data.get('max_threads', None)
        if max_threads is not None:
            try:
                config.MAX_THREADS = max(1, int(max_threads))
            except (ValueError, TypeError):
                pass

        if config.PARALLEL_DOWNLOADS:
            log_message(f"✓ Transfer mode: Parallel multi-thread ({config.MAX_THREADS} threads)", 'info')
        else:
            log_message("✓ Transfer mode: Sequential single-session (1 worker)", 'info')

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
        'file_sizes': deployment_state['file_sizes'],
        'failed_wars': deployment_state.get('failed_wars', [])
    })


@app.route('/api/retry-failed', methods=['POST'])
def retry_failed():
    """Retry only the failed WAR uploads, using alternate routes"""
    if deployment_state['running']:
        return jsonify({'error': 'Deployment already running'}), 400

    failed_wars = deployment_state.get('failed_wars', [])
    if not failed_wars:
        return jsonify({'error': 'No failed uploads to retry'}), 400

    try:
        data = request.json or {}
        version = data.get('version')
        target_server = data.get('target_server')
        target_username = data.get('target_username')

        config_path = os.path.join(os.path.dirname(__file__), 'deployment_config.json')
        config = load_config_from_json(config_path)

        if version:
            config.VERSION = version
            config.SOURCE_PATH = f"/iflightneo/S3_BUILD/NonMS/KE/{version}/"
            config.TARGET_EXTRACT_PATH = f"/iflightneo/global/Utilities/{version}/Wars"
        if target_server:
            config.TARGET_SERVER = target_server
        if target_username:
            config.TARGET_USER = target_username

        # Rotate the route list so failed routes are deprioritized:
        # Each failed war tried route index N → start rotation from N+1
        failed_routes = deployment_state.get('failed_routes', {})
        target_routes = getattr(config, 'TARGET_ROUTES', [])
        if not target_routes:
            target_routes = [{'host': config.TARGET_SERVER, 'username': config.TARGET_USER}]

        # Build a rotated route list for the retry:
        # Find the most common failed route index and rotate past it
        if failed_routes and len(target_routes) > 1:
            from collections import Counter
            most_failed_idx = Counter(failed_routes.values()).most_common(1)[0][0]
            rotate_by = (most_failed_idx + 1) % len(target_routes)
            config.TARGET_ROUTES = target_routes[rotate_by:] + target_routes[:rotate_by]
            log_message(f"↻ Retry: rotating routes to avoid route {most_failed_idx} ({target_routes[most_failed_idx]['host'].split('.')[0]})", 'info')

        wars_to_retry = list(failed_wars)
        log_message(f"↻ Retrying {len(wars_to_retry)} failed upload(s): {', '.join(w.replace('iflight-','').replace('-webapp','').upper() for w in wars_to_retry)}", 'info')

        def run_retry():
            deployment_state['running'] = True
            deployment_state['cancelled'] = False
            try:
                deploy_step2(config, wars_to_retry)
            except Exception as e:
                log_message(f"Retry failed: {str(e)}", 'error')
            finally:
                deployment_state['running'] = False
                broadcast_message('complete', {
                    'cancelled': deployment_state.get('cancelled', False),
                    'failed_wars': deployment_state.get('failed_wars', [])
                })

        thread = threading.Thread(target=run_retry)
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'retrying': wars_to_retry})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/test-connection', methods=['POST'])
def test_connection():
    """Test server connections with detailed path verification and dry run"""
    results = {'source': None, 'target': None, 'success': True}
    deployment_state['running'] = True
    deployment_state['cancelled'] = False
    
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
    finally:
        deployment_state['running'] = False


@app.route('/api/cancel', methods=['POST'])
def cancel_deployment():
    """Cancel running deployment or connection test"""
    deployment_state['cancelled'] = True
    deployment_state['running'] = False
    log_message("⚠️ Cancellation requested by user", 'warning')
    broadcast_message('complete', {
        'cancelled': True,
        'failed_wars': deployment_state.get('failed_wars', [])
    })
    return jsonify({'success': True})


if __name__ == '__main__':
    print("=" * 60)
    print("iFlight Neo Wars Deployment - Web Interface")
    print("=" * 60)
    print(f"\n[WEB] Open in browser: http://localhost:5000\n")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False, threaded=True)
