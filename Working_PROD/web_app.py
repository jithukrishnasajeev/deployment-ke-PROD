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
from deployment_automation import (
    DeploymentConfig, SSHClient, calculate_local_md5, 
    get_remote_md5, load_config_from_json
)

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
    """Step 1: Download selected WAR files with size tracking"""
    try:
        log_message("═" * 50, 'info')
        log_message("STEP 1: Package and Download to Local", 'success')
        log_message("═" * 50, 'info')
        
        # Connect to source server
        log_message(f"Connecting to {config.SOURCE_SERVER}...", 'info')
        ssh = SSHClient(config.SOURCE_SERVER, config.SOURCE_USER, config.SOURCE_PASSWORD)
        ssh.client = ssh.connect().client
        log_message(f"✓ Connected to source server", 'success')
        
        source_wars_dir = f"{config.SOURCE_PATH}Wars"
        os.makedirs(config.LOCAL_DOWNLOAD_PATH, exist_ok=True)
        
        sftp = ssh.get_sftp()
        deployment_state['total_files'] = len(selected_wars)
        deployment_state['completed_files'] = 0
        deployment_state['file_sizes'] = {}
        
        # Process each selected WAR file
        for idx, war_prefix in enumerate(selected_wars, 1):
            if deployment_state.get('cancelled'):
                log_message("⚠ Deployment cancelled", 'warning')
                break
            
            war_file = f"{war_prefix}-{config.VERSION}.war"
            tar_file = f"{war_prefix}-{config.VERSION}.tar.gz"
            war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
            
            update_progress((idx - 1) / len(selected_wars) * 100, war_name)
            update_file_size(war_prefix, war_name, status='processing')
            
            log_message(f"[{idx}/{len(selected_wars)}] {war_name}", 'info')
            
            remote_war = f"{source_wars_dir}/{war_file}"
            remote_tar = f"/tmp/{tar_file}"
            local_tar = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)
            
            # Get source WAR file size
            source_war_size = get_remote_file_size(ssh, remote_war)
            update_file_size(war_prefix, war_name, source_size=source_war_size)
            log_message(f"  📦 Source WAR: {format_size(source_war_size)}", 'info')
            
            # Compress
            log_message(f"  ⚙ Compressing...", 'info')
            stdin, stdout, stderr = ssh.client.exec_command(
                f"cd {source_wars_dir} && tar -czf {remote_tar} {war_file}"
            )
            stdout.channel.recv_exit_status()
            
            # Download
            log_message(f"  ⬇ Downloading...", 'info')
            sftp.get(remote_tar, local_tar)
            
            download_size = os.path.getsize(local_tar)
            log_message(f"  ✓ Downloaded: {format_size(download_size)} (compressed)", 'success')
            
            # Verify
            log_message(f"  🔐 Verifying MD5...", 'info')
            remote_md5 = get_remote_md5(ssh, remote_tar)
            local_md5 = calculate_local_md5(local_tar)
            
            if remote_md5 and local_md5 == remote_md5:
                log_message(f"  ✓ Integrity verified", 'success')
                update_file_size(war_prefix, war_name, status='downloaded')
            else:
                log_message(f"  ⚠ Checksum mismatch!", 'warning')
                update_file_size(war_prefix, war_name, status='warning')
            
            # Cleanup
            ssh.client.exec_command(f"rm -f {remote_tar}")
            
            deployment_state['completed_files'] = idx
            update_progress(idx / len(selected_wars) * 100, war_name)
        
        sftp.close()
        ssh.close()
        
        log_message("═" * 50, 'info')
        log_message("✓ STEP 1 COMPLETED!", 'success')
        return True
        
    except Exception as e:
        log_message(f"✗ Step 1 failed: {str(e)}", 'error')
        return False


def deploy_step2(config, selected_wars):
    """Step 2: Upload and deploy selected WAR files with size tracking"""
    try:
        log_message("═" * 50, 'info')
        log_message("STEP 2: Upload and Deploy WAR files", 'success')
        log_message("═" * 50, 'info')
        
        # Connect to target server
        log_message(f"Connecting to {config.TARGET_SERVER}...", 'info')
        ssh = SSHClient(config.TARGET_SERVER, config.TARGET_USER, config.TARGET_PASSWORD)
        ssh.client = ssh.connect().client
        log_message(f"✓ Connected to target server", 'success')
        
        sftp = ssh.get_sftp()
        ssh.client.exec_command(f"mkdir -p {config.TARGET_EXTRACT_PATH}")
        
        deployment_state['total_files'] = len(selected_wars)
        deployment_state['completed_files'] = 0
        
        war_map = dict(config.WAR_MAPPINGS)
        
        for idx, war_prefix in enumerate(selected_wars, 1):
            if deployment_state.get('cancelled'):
                log_message("⚠ Deployment cancelled", 'warning')
                break
            
            war_file = f"{war_prefix}-{config.VERSION}.war"
            tar_file = f"{war_prefix}-{config.VERSION}.tar.gz"
            war_name = war_prefix.replace('iflight-', '').replace('-webapp', '').upper()
            
            update_progress((idx - 1) / len(selected_wars) * 100, war_name)
            update_file_size(war_prefix, war_name, status='uploading')
            
            log_message(f"[{idx}/{len(selected_wars)}] {war_name}", 'info')
            
            local_tar = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)
            target_tar_path = f"/tmp/{tar_file}"
            
            if not os.path.exists(local_tar):
                log_message(f"  ✗ Local file not found", 'error')
                update_file_size(war_prefix, war_name, status='error')
                continue
            
            # Upload
            upload_size = os.path.getsize(local_tar)
            log_message(f"  ⬆ Uploading {format_size(upload_size)}...", 'info')
            sftp.put(local_tar, target_tar_path)
            log_message(f"  ✓ Uploaded", 'success')
            
            # Verify
            log_message(f"  🔐 Verifying MD5...", 'info')
            local_md5 = calculate_local_md5(local_tar)
            remote_md5 = get_remote_md5(ssh, target_tar_path)
            
            if remote_md5 and local_md5 == remote_md5:
                log_message(f"  ✓ Integrity verified", 'success')
            
            # Extract
            log_message(f"  📂 Extracting...", 'info')
            stdin, stdout, stderr = ssh.client.exec_command(
                f"cd {config.TARGET_EXTRACT_PATH} && tar -xzf {target_tar_path}"
            )
            stdout.channel.recv_exit_status()
            
            # Get deployed WAR size
            deployed_war = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
            target_war_size = get_remote_file_size(ssh, deployed_war)
            update_file_size(war_prefix, war_name, target_size=target_war_size)
            log_message(f"  📦 Target WAR: {format_size(target_war_size)}", 'info')
            
            # Deploy to final location
            deploy_folder = war_map.get(war_prefix)
            if deploy_folder:
                target_dir = f"{config.TARGET_DEPLOY_BASE}/{deploy_folder}/{config.VERSION}/War"
                ssh.client.exec_command(f"mkdir -p {target_dir}")
                ssh.client.exec_command(f"cp {deployed_war} {target_dir}/")
                
                # Verify final deployed size
                final_war = f"{target_dir}/{war_file}"
                final_size = get_remote_file_size(ssh, final_war)
                update_file_size(war_prefix, war_name, target_size=final_size, status='deployed')
                
                log_message(f"  ✓ Deployed to {deploy_folder} ({format_size(final_size)})", 'success')
            
            # Cleanup
            ssh.client.exec_command(f"rm -f {target_tar_path}")
            
            deployment_state['completed_files'] = idx
            update_progress(idx / len(selected_wars) * 100, war_name)
        
        sftp.close()
        ssh.close()
        
        log_message("═" * 50, 'info')
        log_message("✓ STEP 2 COMPLETED!", 'success')
        log_message("═" * 50, 'success')
        log_message("🚀 DEPLOYMENT COMPLETED!", 'success')
        return True
        
    except Exception as e:
        log_message(f"✗ Step 2 failed: {str(e)}", 'error')
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
        
        return jsonify({
            'version': config.VERSION,
            'source_server': config.SOURCE_SERVER,
            'target_server': config.TARGET_SERVER,
            'target_username': config.TARGET_USER,
            'local_path': config.LOCAL_DOWNLOAD_PATH,
            'war_files': [prefix for prefix, _ in config.WAR_MAPPINGS]
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
        sample_tar = f"iflight-crew-cwp-webapp-{config.VERSION}.tar.gz"
        
        log_message(f"1. cd {config.SOURCE_PATH}Wars", 'info')
        log_message(f"2. tar -czf /tmp/{sample_tar} {sample_war}", 'info')
        log_message(f"3. scp /tmp/{sample_tar} local:{config.LOCAL_DOWNLOAD_PATH}/", 'info')
        log_message(f"4. md5sum /tmp/{sample_tar}", 'info')
        log_message(f"5. rm -f /tmp/{sample_tar}", 'info')
        
        log_message("─" * 40, 'info')
        log_message("📋 STEP 2 Commands (per WAR file):", 'info')
        
        log_message(f"1. scp {config.LOCAL_DOWNLOAD_PATH}/{sample_tar} target:/tmp/", 'info')
        log_message(f"2. md5sum /tmp/{sample_tar}", 'info')
        log_message(f"3. cd {config.TARGET_EXTRACT_PATH} && tar -xzf /tmp/{sample_tar}", 'info')
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
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
