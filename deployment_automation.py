#!/usr/bin/env python3
"""
iFlight Neo Wars Deployment Automation Script
Automates the process of packaging, transferring, and deploying .war files
"""

import paramiko
import os
import sys
import getpass
import time
import json
import hashlib
from pathlib import Path
from dotenv import load_dotenv
import concurrent.futures
# Load environment variables from .env file
load_dotenv()


class DeploymentConfig:
    """Configuration for deployment"""
    
    # Step 1: Source Server
    SOURCE_SERVER = "10.246.26.148"
    SOURCE_USER = "your_username"  # Replace with actual username
    SOURCE_SWITCH_USER = "iflight_user"
    
    # Build path and version
    VERSION = "3.96.34.244"
    SOURCE_PATH = f"/iflightneo/S3_BUILD/NonMS/KE/{VERSION}/"
    TAR_FILE = "Wars.tar.gz"
    
    # SFTP Server
    SFTP_SERVER = "sftp.ibsplc.com"
    SFTP_USER = "a-10266"
    SFTP_REMOTE_PATH = f"iFlight Neo/PST/JITHU/Release/{VERSION}"
    
    # Step 2: Target Server (via SFTP proxy)
    TARGET_SERVER = "10.175.1.247"
    TARGET_USER = "a-10266@ibsplc.com%iflightkeprod%10.175.1.247"
    
    # Target paths
    TARGET_EXTRACT_PATH = f"/iflightneo/global/Utilities/{VERSION}/Wars"
    TARGET_DEPLOY_BASE = f"/iflightneo/global/PROD/ifl_prod_KE_crew/NonMS/Deployments"
    
    # WAR file mappings: (war_file_prefix, deployment_folder)
    WAR_MAPPINGS = [
        ("iflight-crew-cwp-webapp", "CREW_CWP"),
        ("iflight-crew-dsm-webapp", "CREW_DSM"),
        ("iflight-crew-integration-webapp", "CREW_INTEGRATION"),
        ("iflight-crew-messaging-webapp", "CREW_MSG"),
        ("iflight-crew-mobility-webapp", "CREW_MOBILITY"),
        ("iflight-crew-notification-webapp", "CREW_NOTIF"),
        ("iflight-crew-rules-webapp", "CREW_RULE"),
        ("iflight-crew-scheduler-webapp", "CREW_SCHED"),
        ("iflight-crew-stats-webapp", "CREW_STATS"),
        ("iflight-crew-webapp", "CREW_WEB"),
    ]


class SSHClient:
    """SSH Client wrapper for remote operations"""
    
    def __init__(self, hostname, username, password, port=22):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.port = port
        self.client = None
        self.channel = None
    
    def connect(self):
        """Establish SSH connection"""
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        print(f"[INFO] Connecting to {self.hostname}...")
        self.client.connect(
            hostname=self.hostname,
            username=self.username,
            password=self.password,
            port=self.port,
            timeout=30
        )
        print(f"[SUCCESS] Connected to {self.hostname}")
        return self
    
    def execute_command(self, command, sudo_password=None, timeout=300):
        """Execute a command on remote server"""
        print(f"[CMD] {command}")
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        
        if sudo_password and 'sudo' in command:
            stdin.write(sudo_password + '\n')
            stdin.flush()
        
        output = stdout.read().decode('utf-8')
        error = stderr.read().decode('utf-8')
        exit_code = stdout.channel.recv_exit_status()
        
        if output:
            print(f"[OUTPUT] {output}")
        if error and exit_code != 0:
            print(f"[ERROR] {error}")
        
        return output, error, exit_code
    
    def execute_as_user(self, command, switch_user, password):
        """Execute command as different user using sudo su"""
        full_command = f"echo '{password}' | sudo -S su - {switch_user} -c '{command}'"
        return self.execute_command(full_command)
    
    def get_sftp(self):
        """Get SFTP client"""
        return self.client.open_sftp()
    
    def close(self):
        """Close SSH connection"""
        if self.client:
            self.client.close()
            print(f"[INFO] Disconnected from {self.hostname}")


class SFTPClient:
    """SFTP Client for file transfers"""
    
    def __init__(self, hostname, username, password, port=22):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.port = port
        self.transport = None
        self.sftp = None
    
    def connect(self):
        """Establish SFTP connection"""
        print(f"[INFO] Connecting to SFTP {self.hostname}...")
        self.transport = paramiko.Transport((self.hostname, self.port))
        self.transport.connect(username=self.username, password=self.password)
        self.sftp = paramiko.SFTPClient.from_transport(self.transport)
        print(f"[SUCCESS] SFTP connected to {self.hostname}")
        return self
    
    def mkdir_p(self, remote_path):
        """Create directory recursively if not exists"""
        dirs = remote_path.split('/')
        current_path = ''
        for d in dirs:
            if not d:
                continue
            current_path += '/' + d
            try:
                self.sftp.stat(current_path)
            except FileNotFoundError:
                print(f"[INFO] Creating directory: {current_path}")
                self.sftp.mkdir(current_path)
    
    def upload(self, local_path, remote_path):
        """Upload file to SFTP server"""
        print(f"[UPLOAD] {local_path} -> {remote_path}")
        # Ensure directory exists
        remote_dir = os.path.dirname(remote_path)
        self.mkdir_p(remote_dir)
        self.sftp.put(local_path, remote_path, callback=self.progress_callback)
        print(f"[SUCCESS] Uploaded {local_path}")
    
    def download(self, remote_path, local_path):
        """Download file from SFTP server"""
        print(f"[DOWNLOAD] {remote_path} -> {local_path}")
        self.sftp.get(remote_path, local_path, callback=self.progress_callback)
        print(f"[SUCCESS] Downloaded to {local_path}")
    
    def progress_callback(self, transferred, total):
        """Progress callback for file transfers"""
        percentage = (transferred / total) * 100
        print(f"\r[PROGRESS] {percentage:.1f}% ({transferred}/{total} bytes)", end='')
        if transferred == total:
            print()
    
    def close(self):
        """Close SFTP connection"""
        if self.sftp:
            self.sftp.close()
        if self.transport:
            self.transport.close()
        print(f"[INFO] SFTP disconnected from {self.hostname}")


def calculate_local_md5(file_path):
    """Calculate MD5 checksum of local file"""
    md5_hash = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()


def get_remote_md5(ssh_client, file_path):
    """Get MD5 checksum of remote file"""
    output, error, exit_code = ssh_client.execute_command(f"md5sum {file_path}")
    if exit_code == 0:
        # md5sum output format: "checksum  filename"
        return output.split()[0]
    return None


def step1_package_and_upload(config, source_password, sftp_password):
    """
    Step 1: Connect to source server, compress each WAR individually, and download to local
    """
    print("\n" + "="*60)
    print("STEP 1: Package Wars and Download to Local")
    print("="*60 + "\n")
    
    # Connect to source server
    ssh = SSHClient(config.SOURCE_SERVER, config.SOURCE_USER, source_password)
    ssh.connect()
    
    try:
        source_wars_dir = f"{config.SOURCE_PATH}Wars"
        
        # Check if Wars directory exists
        output, error, exit_code = ssh.execute_command(f"ls -la {source_wars_dir}/ | head -5")
        if exit_code != 0:
            print(f"[ERROR] Wars directory not found at {source_wars_dir}")
            raise FileNotFoundError(f"Wars directory not found: {source_wars_dir}")
        
        # Ensure local directory exists
        os.makedirs(config.LOCAL_DOWNLOAD_PATH, exist_ok=True)
        
        # Get SFTP connection
        sftp = ssh.get_sftp()
        
        print(f"[INFO] Compressing and downloading {len(config.WAR_MAPPINGS)} WAR files individually...\n")
        
        downloaded_files = []
        
        # Process each WAR file individually
        for idx, (war_prefix, deploy_folder) in enumerate(config.WAR_MAPPINGS, 1):
            war_file = f"{war_prefix}-{config.VERSION}.war"
            tar_file = f"{war_prefix}-{config.VERSION}.tar.gz"
            remote_tar = f"/tmp/{tar_file}"
            local_tar = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)
            
            print(f"[{idx}/{len(config.WAR_MAPPINGS)}] Processing {war_file}")
            
            # Compress individual WAR file
            print(f"  [INFO] Compressing {war_file}...")
            output, error, exit_code = ssh.execute_command(
                f"cd {source_wars_dir} && tar -czf {remote_tar} {war_file}"
            )
            
            if exit_code != 0:
                print(f"  [ERROR] Failed to compress {war_file}: {error}")
                continue
            
            # Download compressed file
            print(f"  [INFO] Downloading {tar_file}...")
            
            def progress(transferred, total):
                percentage = (transferred / total) * 100
                mb_transferred = transferred / (1024*1024)
                mb_total = total / (1024*1024)
                print(f"\r  [PROGRESS] {percentage:.1f}% ({mb_transferred:.1f}/{mb_total:.1f} MB)", end='')
                if transferred == total:
                    print()
            
            try:
                sftp.get(remote_tar, local_tar, callback=progress)
                file_size = os.path.getsize(local_tar)
                print(f"  [SUCCESS] Downloaded ({file_size / (1024*1024):.2f} MB)")
                
                # Verify checksum
                print(f"  [INFO] Verifying integrity...")
                remote_md5 = get_remote_md5(ssh, remote_tar)
                local_md5 = calculate_local_md5(local_tar)
                
                if remote_md5 and local_md5 == remote_md5:
                    print(f"  [SUCCESS] Integrity verified ✓")
                    downloaded_files.append(local_tar)
                else:
                    print(f"  [WARNING] Checksum mismatch!")
                
                # Cleanup compressed file from /tmp on source server
                ssh.execute_command(f"rm -f {remote_tar}")
                
            except Exception as e:
                print(f"  [ERROR] Download failed: {e}")
            
            print()
        
        sftp.close()
        
    finally:
        ssh.close()
    
    print("\n[SUCCESS] Step 1 completed!")
    print(f"  Downloaded {len(downloaded_files)} files to: {config.LOCAL_DOWNLOAD_PATH}")


def step2_download_and_deploy(config, target_password, sftp_password):
    """
    Step 2: Upload from local to target server, extract, and deploy WAR files (Concurrent)
    """
    print("\n" + "="*60)
    print("STEP 2: Upload to Target Server and Deploy WAR files (Concurrent)")
    print("="*60 + "\n")
    
    # Connect to target server (Main thread for setup and final deployment)
    main_ssh = SSHClient(config.TARGET_SERVER, config.TARGET_USER, target_password)
    main_ssh.connect()
    
    try:
        # Create extraction directory
        main_ssh.execute_command(f"mkdir -p {config.TARGET_EXTRACT_PATH}")
        
        print(f"[INFO] Concurrently uploading and extracting {len(config.WAR_MAPPINGS)} WAR files...\n")
        
        # --- THREAD WORKER FUNCTION ---
        def process_war(item):
            idx, (war_prefix, deploy_folder) = item
            war_file = f"{war_prefix}-{config.VERSION}.war"
            tar_file = f"{war_prefix}-{config.VERSION}.tar.gz"
            local_tar = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)
            target_tar_path = f"/tmp/{tar_file}"
            
            # Check if local file exists
            if not os.path.exists(local_tar):
                return f"  [{idx}] [ERROR] Local file not found: {local_tar}"
                
            file_size = os.path.getsize(local_tar) / (1024*1024)
            print(f"  [{idx}/{len(config.WAR_MAPPINGS)}] Starting upload of {tar_file} ({file_size:.2f} MB)...")
            
            # Spin up a dedicated raw Paramiko SSH client for this thread
            # This bypasses your SSHClient wrapper to keep the terminal clean from connection logs
            thread_client = paramiko.SSHClient()
            thread_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            try:
                thread_client.connect(
                    hostname=config.TARGET_SERVER,
                    username=config.TARGET_USER,
                    password=target_password,
                    port=22,
                    timeout=30,
                    compress=False,
                    ciphers=['aes128-ctr', 'chacha20-poly1305@openssh.com', 'aes128-cbc']
                )
                thread_sftp = thread_client.open_sftp()
                
                # The Speed Hack: confirm=False
                thread_sftp.put(local_tar, target_tar_path, confirm=False)
                
                # MD5 Verification
                local_md5 = calculate_local_md5(local_tar)
                stdin, stdout, stderr = thread_client.exec_command(f"md5sum {target_tar_path}")
                remote_md5_output = stdout.read().decode('utf-8')
                remote_md5 = remote_md5_output.split()[0] if remote_md5_output else None
                
                if not remote_md5 or local_md5 != remote_md5:
                    return f"  [{idx}] [WARNING] Checksum mismatch for {tar_file}!"
                
                # Extract and cleanup
                thread_client.exec_command(f"cd {config.TARGET_EXTRACT_PATH} && tar -xzf {target_tar_path}")
                thread_client.exec_command(f"rm -f {target_tar_path}")
                
                return f"  [{idx}] [SUCCESS] Uploaded, verified, and extracted {tar_file} ✓"
                
            except Exception as e:
                return f"  [{idx}] [ERROR] Failed on {tar_file}: {e}"
            finally:
                thread_client.close()

        # --- RUN CONCURRENT UPLOADS ---
        # max_workers=5 means 5 WAR files are uploaded at the exact same time
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            # Pass the index along with the mapping to keep your [1/10] formatting
            items_to_process = list(enumerate(config.WAR_MAPPINGS, 1))
            results = list(executor.map(process_war, items_to_process))
            
        print("\n[INFO] Concurrent upload phase complete. Results:")
        for result_msg in results:
            print(result_msg)

        # --- SEQUENTIAL DEPLOYMENT ---
        # List extracted files
        main_ssh.execute_command(f"ls -la {config.TARGET_EXTRACT_PATH}/")
        
        # Create all deployment directories first
        print(f"\n[INFO] Creating deployment directories...")
        main_ssh.execute_command(f"cd {config.TARGET_DEPLOY_BASE}")
        
        for war_prefix, deploy_folder in config.WAR_MAPPINGS:
            dir_path = f"{deploy_folder}/{config.VERSION}/War"
            main_ssh.execute_command(f"cd {config.TARGET_DEPLOY_BASE} && mkdir -p {dir_path}")
        
        print(f"[SUCCESS] All deployment directories created")
        
        # Deploy each WAR file
        for war_prefix, deploy_folder in config.WAR_MAPPINGS:
            war_file = f"{war_prefix}-{config.VERSION}.war"
            source_war = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
            target_dir = f"{config.TARGET_DEPLOY_BASE}/{deploy_folder}/{config.VERSION}/War"
            
            deploy_commands = [
                f"cp {source_war} {target_dir}/",
                f"ls -la {target_dir}/",
            ]
            
            print(f"\n[DEPLOY] Deploying {war_file} to {deploy_folder}")
            for cmd in deploy_commands:
                main_ssh.execute_command(cmd)
        
        # Cleanup any remaining compressed files on server
        main_ssh.execute_command(f"rm -f /tmp/*.tar.gz /tmp/*.war")
        
    finally:
        main_ssh.close()
    
    # Ask if user wants to clean up local files
    cleanup = input(f"\nDelete all local compressed files? [y/N]: ").strip().lower()
    if cleanup == 'y':
        removed_count = 0
        for war_prefix, deploy_folder in config.WAR_MAPPINGS:
            tar_file = f"{war_prefix}-{config.VERSION}.tar.gz"
            local_tar = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)
            if os.path.exists(local_tar):
                os.remove(local_tar)
                removed_count += 1
        print(f"[INFO] Cleaned up {removed_count} local compressed files")
    else:
        print(f"[INFO] Local files kept at: {config.LOCAL_DOWNLOAD_PATH}")
    
    print("\n[SUCCESS] Step 2 completed!")

def load_config_from_json(config_path):
    """Load configuration from JSON file"""
    with open(config_path, 'r') as f:
        json_config = json.load(f)
    
    config = DeploymentConfig()
    
    # Version
    config.VERSION = json_config.get('version', config.VERSION)
    
    # Source server
    source = json_config.get('source_server', {})
    config.SOURCE_SERVER = source.get('host', config.SOURCE_SERVER)
    config.SOURCE_USER = source.get('username', config.SOURCE_USER)
    # Load password from environment variable instead of JSON
    config.SOURCE_PASSWORD = os.getenv('SOURCE_SERVER_PASSWORD', source.get('password', ''))
    config.SOURCE_SWITCH_USER = source.get('switch_user', config.SOURCE_SWITCH_USER)
    
    # Local download path
    local = json_config.get('local', {})
    config.LOCAL_DOWNLOAD_PATH = local.get('download_path', os.getcwd())
    
    # Target server
    target = json_config.get('target_server', {})
    
    # Fallback to single host/user if routes aren't defined
    default_route = {
        'host': target.get('host', config.TARGET_SERVER),
        'username': target.get('username', config.TARGET_USER)
    }
    config.TARGET_ROUTES = target.get('routes', [default_route])
    
    # Keep these for backward compatibility with your other steps
    config.TARGET_SERVER = config.TARGET_ROUTES[0]['host']
    config.TARGET_USER = config.TARGET_ROUTES[0]['username']
    config.TARGET_PASSWORD = os.getenv('TARGET_SERVER_PASSWORD', target.get('password', ''))
    
    # Paths
    paths = json_config.get('paths', {})
    source_base = paths.get('source_base', '/iflightneo/S3_BUILD/NonMS/KE')
    config.SOURCE_PATH = f"{source_base}/{config.VERSION}/"
    target_utilities = paths.get('target_utilities', '/iflightneo/global/Utilities')
    config.TARGET_EXTRACT_PATH = f"{target_utilities}/{config.VERSION}/Wars"
    config.TARGET_DEPLOY_BASE = paths.get('target_deploy_base', config.TARGET_DEPLOY_BASE)
    
    # WAR mappings
    war_mappings = json_config.get('war_mappings', {})
    if war_mappings:
        config.WAR_MAPPINGS = [(k, v) for k, v in war_mappings.items()]
    
    # Transfer optimization (SCP support)
    transfer_opt = json_config.get('transfer_optimization', {})
    config.USE_SCP = transfer_opt.get('protocol', 'SFTP').upper() == 'SCP' and transfer_opt.get('enabled', False)
    
    return config


def interactive_mode(config_path=None):
    """Run deployment in interactive mode with prompts"""
    
    # Load from JSON if provided
    if config_path and os.path.exists(config_path):
        print(f"[INFO] Loading configuration from {config_path}")
        config = load_config_from_json(config_path)
        source_password = getattr(config, 'SOURCE_PASSWORD', '')
        target_password = getattr(config, 'TARGET_PASSWORD', '')
    else:
        config = DeploymentConfig()
        config.LOCAL_DOWNLOAD_PATH = os.getcwd()
        source_password = ''
        target_password = ''
    
    print("\n" + "="*60)
    print("iFlight Neo Wars Deployment Automation")
    print("="*60)
    
    # Get version (allow override)
    version_input = input(f"\nEnter version [{config.VERSION}]: ").strip()
    if version_input:
        config.VERSION = version_input
        # Update dependent paths
        config.SOURCE_PATH = f"/iflightneo/S3_BUILD/NonMS/KE/{config.VERSION}/"
        config.TARGET_EXTRACT_PATH = f"/iflightneo/global/Utilities/{config.VERSION}/Wars"
    
    # Only prompt for credentials if not loaded from config
    if not source_password:
        print("\n--- Step 1 Credentials (Source Server) ---")
        source_user = input(f"Source server username [{config.SOURCE_USER}]: ").strip()
        if source_user:
            config.SOURCE_USER = source_user
        source_password = getpass.getpass("Source server password: ")
    
    if not target_password:
        print("\n--- Step 2 Credentials (Target Server) ---")
        target_user = input(f"Target server username [{config.TARGET_USER}]: ").strip()
        if target_user:
            config.TARGET_USER = target_user
        target_password = getpass.getpass("Target server password: ")
    
    # Confirm
    print("\n" + "-"*60)
    print("Configuration Summary:")
    print(f"  Version: {config.VERSION}")
    print(f"  Source Server: {config.SOURCE_SERVER} (user: {config.SOURCE_USER})")
    print(f"  Local Download: {config.LOCAL_DOWNLOAD_PATH}")
    print(f"  Target Server: {config.TARGET_SERVER}")
    print("-"*60)
    
    confirm = input("\nProceed with deployment? [y/N]: ").strip().lower()
    if confirm != 'y':
        print("Deployment cancelled.")
        return
    
    # Execute steps
    try:
        step1_package_and_upload(config, source_password, None)
        step2_download_and_deploy(config, target_password, None)
        
        print("\n" + "="*60)
        print("DEPLOYMENT COMPLETED SUCCESSFULLY!")
        print("="*60)
        
    except Exception as e:
        print(f"\n[ERROR] Deployment failed: {e}")
        raise


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='iFlight Neo Wars Deployment Automation')
    parser.add_argument('--version', '-v', help='Version number (e.g., 3.96.34.244)')
    parser.add_argument('--step', type=int, choices=[1, 2], help='Run only specific step (1 or 2)')
    parser.add_argument('--config', '-c', help='Path to config file (JSON)', 
                        default='deployment_config.json')
    
    args = parser.parse_args()
    
    # Find config file
    config_path = args.config
    if not os.path.isabs(config_path):
        # Look in script directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, config_path)
    
    interactive_mode(config_path)


if __name__ == "__main__":
    main()
