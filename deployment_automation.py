#!/usr/bin/env python3
"""
iFlight Neo Wars Deployment Automation Script
Automates the process of packaging, transferring, and deploying .war files.
Supports single-session sequential and multi-threaded parallel download modes.
"""

import paramiko
import os
import sys
import getpass
import time
import json
import hashlib

from dotenv import load_dotenv
import concurrent.futures
import socket
import threading

# Load environment variables from .env file
load_dotenv()


class DeploymentConfig:
    """Configuration for deployment"""
    
    # Step 1: Source Server
    SOURCE_SERVER = "10.246.26.148"
    SOURCE_USER = "a-10266"
    SOURCE_SWITCH_USER = "iflight_user"
    SOURCE_PASSWORD = os.getenv('SOURCE_SERVER_PASSWORD', '')
    
    # Build path and version
    VERSION = "3.96.34.244"
    SOURCE_PATH = f"/iflightneo/S3_BUILD/NonMS/KE/{VERSION}/"
    TAR_FILE = "Wars.tar"
    
    # Local download path
    LOCAL_DOWNLOAD_PATH = os.path.join(os.getcwd(), "downloads")
    
    # SFTP Server
    SFTP_SERVER = "sftp.ibsplc.com"
    SFTP_USER = "a-10266"
    SFTP_REMOTE_PATH = f"iFlight Neo/PST/JITHU/Release/{VERSION}"
    
    # Step 2: Target Server (via SFTP proxy)
    TARGET_SERVER = "10.175.1.247"
    TARGET_USER = "a-10266@ibsplc.com%iflightkeprod%10.175.1.247"
    TARGET_PASSWORD = os.getenv('TARGET_SERVER_PASSWORD', '')
    TARGET_ROUTES = []
    
    # Target paths
    TARGET_EXTRACT_PATH = f"/iflightneo/global/Utilities/{VERSION}/Wars"
    TARGET_DEPLOY_BASE = f"/iflightneo/global/PROD/ifl_prod_KE_crew/NonMS/Deployments"
    
    # Transfer optimizations
    PARALLEL_DOWNLOADS = False  # Single SSH session by default; set True for multi-thread parallel
    MAX_THREADS = 4
    DIRECT_WAR_DOWNLOAD = True
    USE_SCP = False
    
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
    """SSH Client wrapper for remote operations with optimized socket buffers"""
    
    def __init__(self, hostname, username, password, port=22):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.port = int(port) if port else 22
        self.client = None
        self.channel = None
        self._transport = None
        self._sock = None
    
    def connect(self, max_retries=3):
        """Establish SSH connection with socket buffer tuning and pre-client window sizing."""
        print(f"[INFO] Connecting to {self.hostname}:{self.port}...")

        last_error = None
        for attempt in range(max_retries):
            if attempt > 0:
                wait = 3 * attempt
                print(f"[INFO] Retrying {self.hostname} (attempt {attempt+1}/{max_retries}) after {wait}s...")
                time.sleep(wait)

            sock = None
            transport = None
            try:
                sock = socket.create_connection((self.hostname, self.port), timeout=30)
                # Socket tuning for high-throughput TCP
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4194304)  # 4 MB receive buffer
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4194304)  # 4 MB send buffer
                except Exception:
                    pass

                transport = paramiko.Transport(sock)
                
                # === SSH TRANSPORT SPEED TUNING (Set BEFORE start_client) ===
                transport.default_window_size = 67108864        # 64 MB window
                transport.default_max_packet_size = 65536       # 64 KB max packet
                transport.set_keepalive(15)
                transport.banner_timeout = 30
                transport.auth_timeout = 60
                transport.packetizer.REKEY_BYTES = pow(2, 30)   # 1 GB before rekey
                transport.packetizer.REKEY_PACKETS = pow(2, 30) # ~1 billion packets

                transport.start_client(timeout=30)

                def ki_handler(title, instructions, prompt_list):
                    return [self.password] * len(prompt_list)

                try:
                    transport.auth_interactive(self.username, ki_handler)
                except paramiko.ssh_exception.BadAuthenticationType:
                    # Server explicitly doesn't support ki — try plain password instead
                    transport.auth_password(self.username, self.password)

                if not transport.is_authenticated():
                    raise paramiko.ssh_exception.AuthenticationException(
                        f"Authentication failed for {self.username}@{self.hostname}"
                    )

                # Success — wire up the SSHClient wrapper
                self.client = paramiko.SSHClient()
                self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.client._transport = transport
                self._transport = transport
                self._sock = sock

                print(f"[SUCCESS] Connected to {self.hostname} (socket & transport tuned)")
                return self

            except paramiko.ssh_exception.AuthenticationException:
                if transport:
                    transport.close()
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                raise

            except Exception as e:
                last_error = e
                if transport:
                    try:
                        transport.close()
                    except Exception:
                        pass
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

        raise last_error
    
    def execute_command(self, command, sudo_password=None, timeout=300):
        """Execute a command on remote server using transport session"""
        print(f"[CMD] {command}")
        
        channel = self._transport.open_session()
        channel.settimeout(timeout)
        channel.exec_command(command)
        
        if sudo_password and 'sudo' in command:
            channel.send(sudo_password + '\n')
        
        stdout_data = b''
        stderr_data = b''
        
        while True:
            if channel.recv_ready():
                stdout_data += channel.recv(65536)
            if channel.recv_stderr_ready():
                stderr_data += channel.recv_stderr(65536)
            if channel.exit_status_ready():
                while channel.recv_ready():
                    stdout_data += channel.recv(65536)
                while channel.recv_stderr_ready():
                    stderr_data += channel.recv_stderr(65536)
                break
        
        output = stdout_data.decode('utf-8', errors='replace')
        error = stderr_data.decode('utf-8', errors='replace')
        exit_code = channel.recv_exit_status()
        channel.close()
        
        if output:
            print(f"[OUTPUT] {output.strip()}")
        if error and exit_code != 0:
            print(f"[ERROR] {error.strip()}")
        
        return output, error, exit_code
    
    def exec_command(self, command, timeout=300):
        """Execute command and return (stdin, stdout, stderr) channel files for compatibility."""
        channel = self._transport.open_session()
        channel.settimeout(timeout)
        channel.exec_command(command)
        
        stdin  = channel.makefile_stdin('wb')
        stdout = channel.makefile('rb')
        stderr = channel.makefile_stderr('rb')
        
        return stdin, stdout, stderr
    
    def execute_as_user(self, command, switch_user, password):
        """Execute command as different user using sudo su"""
        full_command = f"echo '{password}' | sudo -S su - {switch_user} -c '{command}'"
        return self.execute_command(full_command)
    
    def get_sftp(self):
        """Get SFTP client from transport"""
        return paramiko.SFTPClient.from_transport(self._transport)
    
    def get_transport(self):
        """Get the underlying transport for SCP operations"""
        return self._transport
    
    def close(self):
        """Close SSH connection"""
        if hasattr(self, '_transport') and self._transport:
            try:
                self._transport.close()
            except Exception:
                pass
        if hasattr(self, '_sock') and self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
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
        sock = socket.create_connection((self.hostname, self.port), timeout=30)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.transport = paramiko.Transport(sock)
        self.transport.default_window_size = 67108864
        self.transport.default_max_packet_size = 65536
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
        remote_dir = os.path.dirname(remote_path)
        self.mkdir_p(remote_dir)
        self.sftp.put(local_path, remote_path, callback=self.progress_callback, confirm=False)
        print(f"[SUCCESS] Uploaded {local_path}")
    
    def download(self, remote_path, local_path):
        """Download file from SFTP server"""
        print(f"[DOWNLOAD] {remote_path} -> {local_path}")
        md5, size = fast_sftp_download(self.sftp, remote_path, local_path, self.progress_callback)
        print(f"[SUCCESS] Downloaded to {local_path} ({size / (1024*1024):.1f} MB)")
        return md5, size
    
    def progress_callback(self, transferred, total):
        """Progress callback for file transfers"""
        percentage = (transferred / total) * 100 if total > 0 else 0
        mb_trans = transferred / (1024 * 1024)
        mb_tot = total / (1024 * 1024)
        print(f"\r[PROGRESS] {percentage:.1f}% ({mb_trans:.1f}/{mb_tot:.1f} MB)", end='')
        if transferred == total:
            print()
    
    def close(self):
        """Close SFTP connection"""
        if self.sftp:
            try:
                self.sftp.close()
            except Exception:
                pass
        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass
        print(f"[INFO] SFTP disconnected from {self.hostname}")


def fast_sftp_download(sftp, remote_path, local_path, progress_callback=None, cancel_check=None):
    """High-speed SFTP download using Paramiko sftp.get.

    When cancel_check is provided the download runs in chunked mode so that
    cancellation is checked every 1 MB.
    Returns (md5_hex_digest, file_size_bytes).
    """
    file_size = sftp.stat(remote_path).st_size

    if cancel_check:
        # Chunked mode — checks cancellation every 1 MB
        md5_hash = hashlib.md5()
        transferred = 0
        chunk_size = 1048576
        with sftp.open(remote_path, 'rb') as remote_file:
            with open(local_path, 'wb') as local_file:
                while True:
                    if cancel_check():
                        raise InterruptedError("Download cancelled by caller")
                    data = remote_file.read(chunk_size)
                    if not data:
                        break
                    local_file.write(data)
                    md5_hash.update(data)
                    transferred += len(data)
                    if progress_callback:
                        progress_callback(transferred, file_size)
        return md5_hash.hexdigest(), file_size
    else:
        sftp.get(remote_path, local_path, callback=progress_callback)
        return calculate_local_md5(local_path), file_size


def calculate_local_md5(file_path):
    """Calculate MD5 checksum of local file (1 MB read chunks)"""
    md5_hash = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()


def get_remote_md5(ssh_client, file_path):
    """Get MD5 checksum of remote file"""
    output, error, exit_code = ssh_client.execute_command(f"md5sum '{file_path}'")
    if exit_code == 0 and output.strip():
        return output.split()[0]
    return None


def step1_package_and_upload(config, source_password, sftp_password):
    """
    Step 1: Download WAR files from source server to local machine.

    Mode controlled by config.PARALLEL_DOWNLOADS:
      - False (default): single SSH session, sequential downloads
      - True:  one SSH connection per thread, concurrent downloads
    """
    print("\n" + "="*60)
    print("STEP 1: Download WAR Files to Local")
    print("="*60 + "\n")

    os.makedirs(config.LOCAL_DOWNLOAD_PATH, exist_ok=True)
    source_wars_dir = f"{config.SOURCE_PATH}Wars"

    parallel     = getattr(config, 'PARALLEL_DOWNLOADS', False)
    max_workers  = getattr(config, 'MAX_THREADS', 4) if parallel else 1
    direct_dl    = getattr(config, 'DIRECT_WAR_DOWNLOAD', True)

    mode_str = "Direct WAR (no tar wrapper)" if direct_dl else "Tar archive packaging"
    print(f"[INFO] Download Mode: {mode_str}")
    print(f"[INFO] Thread Mode:   {'Parallel (' + str(max_workers) + ' threads)' if parallel else 'Single SSH Session (sequential)'}\n")

    downloaded_files = []
    print_lock = threading.Lock()

    def _download_one(idx, war_prefix, deploy_folder, ssh_conn, sftp_conn):
        """Download one WAR using provided connections. Returns local path or None."""
        war_file = f"{war_prefix}-{config.VERSION}.war"

        if direct_dl:
            remote_path = f"{source_wars_dir}/{war_file}"
            local_path  = os.path.join(config.LOCAL_DOWNLOAD_PATH, war_file)

            with print_lock:
                print(f"[{idx}/{len(config.WAR_MAPPINGS)}] Downloading: {war_file}")

            remote_md5 = get_remote_md5(ssh_conn, remote_path)
            start_t    = time.time()

            # Default arg captures war_prefix by value — avoids closure bug in loops
            def progress(transferred, total, _prefix=war_prefix):
                pct    = (transferred / total * 100) if total > 0 else 0
                mb_tr  = transferred / (1024 * 1024)
                mb_tot = total / (1024 * 1024)
                print(f"\r  [{_prefix}] {pct:.1f}% ({mb_tr:.1f}/{mb_tot:.1f} MB)", end='')

            local_md5, file_size = fast_sftp_download(sftp_conn, remote_path, local_path, progress_callback=progress)
            elapsed  = max(0.1, time.time() - start_t)
            speed_mb = (file_size / (1024 * 1024)) / elapsed

            with print_lock:
                print(f"\n  [SUCCESS] {war_file} ({file_size/(1024*1024):.2f} MB @ {speed_mb:.2f} MB/s)")
                if remote_md5 and local_md5 == remote_md5:
                    print(f"  [SUCCESS] MD5 verified ({local_md5}) ✓\n")
                else:
                    print(f"  [WARNING] Checksum mismatch!\n")
            return local_path
        else:
            tar_file   = f"{war_prefix}-{config.VERSION}.tar"
            remote_tar = f"/tmp/{tar_file}"
            local_path = os.path.join(config.LOCAL_DOWNLOAD_PATH, tar_file)

            with print_lock:
                print(f"[{idx}/{len(config.WAR_MAPPINGS)}] Packaging + downloading: {tar_file}")

            output, error, exit_code = ssh_conn.execute_command(
                f"cd '{source_wars_dir}' && tar -cf '{remote_tar}' '{war_file}'"
            )
            if exit_code != 0:
                with print_lock:
                    print(f"  [ERROR] Packaging failed for {war_file}: {error}\n")
                return None

            remote_md5 = get_remote_md5(ssh_conn, remote_tar)
            local_md5, _size = fast_sftp_download(sftp_conn, remote_tar, local_path)
            ssh_conn.execute_command(f"rm -f '{remote_tar}'")

            with print_lock:
                if remote_md5 and local_md5 == remote_md5:
                    print(f"  [SUCCESS] Downloaded & Verified {tar_file} ✓\n")
                else:
                    print(f"  [WARNING] Checksum mismatch for {tar_file}!\n")
            return local_path

    if parallel and max_workers > 1:
        def _parallel_worker(item):
            idx, (war_prefix, deploy_folder) = item
            thread_ssh = SSHClient(config.SOURCE_SERVER, config.SOURCE_USER, source_password)
            thread_ssh.connect()
            try:
                thread_sftp = thread_ssh.get_sftp()
                result      = _download_one(idx, war_prefix, deploy_folder, thread_ssh, thread_sftp)
                thread_sftp.close()
                return result
            finally:
                thread_ssh.close()

        items = list(enumerate(config.WAR_MAPPINGS, 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(_parallel_worker, items))
        downloaded_files = [r for r in results if r is not None]
    else:
        # Single SSH session — sequential downloads
        ssh = SSHClient(config.SOURCE_SERVER, config.SOURCE_USER, source_password)
        ssh.connect()
        try:
            sftp = ssh.get_sftp()
            for idx, (war_prefix, deploy_folder) in enumerate(config.WAR_MAPPINGS, 1):
                result = _download_one(idx, war_prefix, deploy_folder, ssh, sftp)
                if result:
                    downloaded_files.append(result)
            sftp.close()
        finally:
            ssh.close()

    print("="*60)
    print(f"[SUCCESS] Step 1 done! {len(downloaded_files)}/{len(config.WAR_MAPPINGS)} files saved to {config.LOCAL_DOWNLOAD_PATH}")
    print("="*60 + "\n")


def step2_download_and_deploy(config, target_password, sftp_password):
    """
    Step 2: Upload local WAR/TAR files to target server, extract if needed, and deploy to target directories.
    """
    print("\n" + "="*60)
    print("STEP 2: Concurrent Upload & Deploy to Target Server")
    print("="*60 + "\n")
    
    main_ssh = SSHClient(config.TARGET_SERVER, config.TARGET_USER, target_password)
    main_ssh.connect()
    
    try:
        main_ssh.execute_command(f"mkdir -p '{config.TARGET_EXTRACT_PATH}'")
        
        max_workers = min(5, len(config.WAR_MAPPINGS))
        print(f"[INFO] Starting concurrent upload & extract with {max_workers} threads...\n")
        
        def upload_worker(item):
            idx, (war_prefix, deploy_folder) = item
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
                return f"  [{idx}] [ERROR] Local file not found for {war_prefix}"
            
            file_size_mb = os.path.getsize(local_file) / (1024 * 1024)
            filename = os.path.basename(local_file)
            
            print(f"  [{idx}/{len(config.WAR_MAPPINGS)}] Uploading {filename} ({file_size_mb:.2f} MB)...")
            
            ssh = SSHClient(config.TARGET_SERVER, config.TARGET_USER, target_password)
            ssh.connect()
            
            try:
                sftp = ssh.get_sftp()
                
                if filename.endswith('.war'):
                    # Direct WAR upload
                    target_remote_path = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
                    sftp.put(local_file, target_remote_path, confirm=False)
                    
                    local_md5 = calculate_local_md5(local_file)
                    remote_md5 = get_remote_md5(ssh, target_remote_path)
                    
                    if remote_md5 and local_md5 != remote_md5:
                        return f"  [{idx}] [WARNING] MD5 mismatch for {war_file}"
                    
                    return f"  [{idx}] [SUCCESS] Uploaded & verified direct WAR {war_file} ✓"
                elif filename.endswith('.zip'):
                    # Upload ZIP file and unzip on target server
                    tmp_zip_path = f"/tmp/{filename}_{threading.current_thread().ident}"
                    sftp.put(local_file, tmp_zip_path, confirm=False)
                    
                    local_md5 = calculate_local_md5(local_file)
                    remote_md5 = get_remote_md5(ssh, tmp_zip_path)
                    
                    if remote_md5 and local_md5 != remote_md5:
                        return f"  [{idx}] [WARNING] MD5 mismatch for {filename}"
                    
                    print(f"  [{idx}] Unzipping {filename} on target server...")
                    unzip_cmd = (
                        f"unzip -o -q '{tmp_zip_path}' -d '{config.TARGET_EXTRACT_PATH}' || "
                        f"python3 -m zipfile -e '{tmp_zip_path}' '{config.TARGET_EXTRACT_PATH}'"
                    )
                    output, error, exit_code = ssh.execute_command(unzip_cmd)
                    ssh.execute_command(f"rm -f '{tmp_zip_path}'")
                    
                    if exit_code != 0:
                        return f"  [{idx}] [ERROR] Unzip failed for {filename}: {error}"
                    
                    return f"  [{idx}] [SUCCESS] Uploaded, verified, & unzipped {filename} ✓"
                else:
                    # Upload tar file to /tmp and extract
                    tmp_tar_path = f"/tmp/{filename}_{threading.current_thread().ident}"
                    sftp.put(local_file, tmp_tar_path, confirm=False)
                    
                    local_md5 = calculate_local_md5(local_file)
                    remote_md5 = get_remote_md5(ssh, tmp_tar_path)
                    
                    if remote_md5 and local_md5 != remote_md5:
                        return f"  [{idx}] [WARNING] MD5 mismatch for {filename}"
                    
                    print(f"  [{idx}] Extracting tar {filename} on target server...")
                    output, error, exit_code = ssh.execute_command(
                        f"cd '{config.TARGET_EXTRACT_PATH}' && tar -xf '{tmp_tar_path}'"
                    )
                    ssh.execute_command(f"rm -f '{tmp_tar_path}'")
                    
                    if exit_code != 0:
                        return f"  [{idx}] [ERROR] Tar extraction failed for {filename}: {error}"
                    
                    return f"  [{idx}] [SUCCESS] Uploaded, verified, & extracted {filename} ✓"
                    
            finally:
                ssh.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            items = list(enumerate(config.WAR_MAPPINGS, 1))
            results = list(executor.map(upload_worker, items))
            
        print("\n[INFO] Upload phase complete. Results:")
        for res in results:
            print(res)

        # Deploy WAR files to final folders
        print("\n[INFO] Deploying WAR files to production directories...")
        for war_prefix, deploy_folder in config.WAR_MAPPINGS:
            war_file = f"{war_prefix}-{config.VERSION}.war"
            source_war = f"{config.TARGET_EXTRACT_PATH}/{war_file}"
            target_dir = f"{config.TARGET_DEPLOY_BASE}/{deploy_folder}/{config.VERSION}/War"
            
            print(f"  [DEPLOY] {war_file} -> {deploy_folder}")
            main_ssh.execute_command(f"mkdir -p '{target_dir}' && cp '{source_war}' '{target_dir}/'")
        
        main_ssh.execute_command("rm -f /tmp/*.tar /tmp/*.war 2>/dev/null || true")
        
    finally:
        main_ssh.close()
    
    print("\n[SUCCESS] Step 2 completed successfully!")


def load_config_from_json(config_path):
    """Load configuration from JSON file"""
    config = DeploymentConfig()
    
    if not os.path.exists(config_path):
        return config
        
    try:
        with open(config_path, 'r') as f:
            json_config = json.load(f)
    except Exception as e:
        print(f"[ERROR] Failed to parse config JSON: {e}")
        return config
    
    # Version
    config.VERSION = json_config.get('version', config.VERSION)
    
    # Source server
    source = json_config.get('source_server', {})
    config.SOURCE_SERVER = source.get('host', config.SOURCE_SERVER)
    config.SOURCE_USER = source.get('username', config.SOURCE_USER)
    config.SOURCE_PASSWORD = os.getenv('SOURCE_SERVER_PASSWORD', source.get('password', ''))
    config.SOURCE_SWITCH_USER = source.get('switch_user', config.SOURCE_SWITCH_USER)
    
    # Local download path
    local = json_config.get('local', {})
    config.LOCAL_DOWNLOAD_PATH = local.get('download_path', os.path.join(os.getcwd(), "downloads"))
    
    # Target server
    target = json_config.get('target_server', {})
    default_route = {
        'host': target.get('host', config.TARGET_SERVER),
        'username': target.get('username', config.TARGET_USER)
    }
    config.TARGET_ROUTES = target.get('routes', [default_route])
    
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
    
    # Transfer optimization
    transfer_opt = json_config.get('transfer_optimization', {})
    config.USE_SCP = transfer_opt.get('protocol', 'SFTP').upper() == 'SCP' and transfer_opt.get('enabled', False)
    config.PARALLEL_DOWNLOADS = transfer_opt.get('parallel_downloads', True)
    config.MAX_THREADS = transfer_opt.get('max_threads', 4)
    config.DIRECT_WAR_DOWNLOAD = transfer_opt.get('direct_war_download', True)
    
    return config


def interactive_mode(config_path=None):
    """Run deployment in interactive mode with prompts"""
    
    if config_path and os.path.exists(config_path):
        print(f"[INFO] Loading configuration from {config_path}")
        config = load_config_from_json(config_path)
    else:
        config = DeploymentConfig()
        
    source_password = getattr(config, 'SOURCE_PASSWORD', '') or os.getenv('SOURCE_SERVER_PASSWORD', '')
    target_password = getattr(config, 'TARGET_PASSWORD', '') or os.getenv('TARGET_SERVER_PASSWORD', '')
    
    print("\n" + "="*60)
    print("iFlight Neo Wars Deployment Automation")
    print("="*60)
    
    version_input = input(f"\nEnter version [{config.VERSION}]: ").strip()
    if version_input:
        import re
        if not re.fullmatch(r'[\d.]+', version_input):
            print("[ERROR] Invalid version format. Use digits and dots only (e.g. 3.96.34.244).")
            return
        config.VERSION = version_input
        config.SOURCE_PATH = f"/iflightneo/S3_BUILD/NonMS/KE/{config.VERSION}/"
        config.TARGET_EXTRACT_PATH = f"/iflightneo/global/Utilities/{config.VERSION}/Wars"
    
    if not source_password:
        print("\n--- Step 1 Credentials (Source Server) ---")
        source_user = input(f"Source server username [{config.SOURCE_USER}]: ").strip()
        if source_user:
            config.SOURCE_USER = source_user
        source_password = getpass.getpass("Source server password: ")
    else:
        config.SOURCE_PASSWORD = source_password
    
    if not target_password:
        print("\n--- Step 2 Credentials (Target Server) ---")
        target_user = input(f"Target server username [{config.TARGET_USER}]: ").strip()
        if target_user:
            config.TARGET_USER = target_user
        target_password = getpass.getpass("Target server password: ")
    else:
        config.TARGET_PASSWORD = target_password
    
    print("\n" + "-"*60)
    print("Configuration Summary:")
    print(f"  Version: {config.VERSION}")
    print(f"  Source Server: {config.SOURCE_SERVER} (user: {config.SOURCE_USER})")
    print(f"  Local Download: {config.LOCAL_DOWNLOAD_PATH}")
    print(f"  Target Server: {config.TARGET_SERVER}")
    print(f"  Direct WAR Download: {getattr(config, 'DIRECT_WAR_DOWNLOAD', True)}")
    print(f"  Parallel Workers: {getattr(config, 'MAX_THREADS', 4)}")
    print("-"*60)
    
    confirm = input("\nProceed with deployment? [y/N]: ").strip().lower()
    if confirm != 'y':
        print("Deployment cancelled.")
        return
    
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
    
    config_path = args.config
    if not os.path.isabs(config_path):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, config_path)
    
    interactive_mode(config_path)


if __name__ == "__main__":
    main()
