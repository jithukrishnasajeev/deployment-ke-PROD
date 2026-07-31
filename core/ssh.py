"""
High-Performance SSH, SFTP, and SCP Transport Module.
Optimized socket buffers, transport window sizes, keyboard-interactive auth, and streaming fast transfers.
"""

import os
import time
import socket
import hashlib
import paramiko
from .utils import calculate_local_md5


class SSHClient:
    """SSH Client wrapper with optimized socket buffers and transport tuning."""

    def __init__(self, hostname: str, username: str, password: str, port: int = 22):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.port = int(port) if port else 22
        self.client = None
        self._transport = None
        self._sock = None

    def connect(self, max_retries: int = 3):
        """Establish SSH connection with TCP buffer tuning and high-throughput transport settings."""
        last_error = None
        for attempt in range(max_retries):
            if attempt > 0:
                wait = 3 * attempt
                time.sleep(wait)

            sock = None
            transport = None
            try:
                sock = socket.create_connection((self.hostname, self.port), timeout=30)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4194304)  # 4 MB receive buffer
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4194304)  # 4 MB send buffer
                except Exception:
                    pass

                transport = paramiko.Transport(sock)
                transport.default_window_size = 67108864        # 64 MB window
                transport.default_max_packet_size = 65536       # 64 KB max packet
                transport.set_keepalive(15)
                transport.banner_timeout = 30
                transport.auth_timeout = 60
                transport.packetizer.REKEY_BYTES = pow(2, 30)   # 1 GB before rekey
                transport.packetizer.REKEY_PACKETS = pow(2, 30)

                transport.start_client(timeout=30)

                def ki_handler(title, instructions, prompt_list):
                    return [self.password] * len(prompt_list)

                try:
                    transport.auth_interactive(self.username, ki_handler)
                except paramiko.ssh_exception.BadAuthenticationType:
                    transport.auth_password(self.username, self.password)

                if not transport.is_authenticated():
                    raise paramiko.ssh_exception.AuthenticationException(
                        f"Authentication failed for {self.username}@{self.hostname}"
                    )

                self.client = paramiko.SSHClient()
                self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.client._transport = transport
                self._transport = transport
                self._sock = sock

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

        if last_error:
            raise last_error
        raise Exception(f"Failed to connect to SSH host {self.hostname}:{self.port}")

    def execute_command(self, command: str, sudo_password: str = None, timeout: int = 300):
        """Execute a remote command and return (stdout_str, stderr_str, exit_code)."""
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

        return output, error, exit_code

    def exec_command(self, command: str, timeout: int = 300):
        """Execute command and return (stdin, stdout, stderr) paramiko channels for streaming."""
        channel = self._transport.open_session()
        channel.settimeout(timeout)
        channel.exec_command(command)

        stdin = channel.makefile_stdin('wb')
        stdout = channel.makefile('rb')
        stderr = channel.makefile_stderr('rb')

        return stdin, stdout, stderr

    def execute_as_user(self, command: str, switch_user: str, password: str):
        """Execute command as a different linux user via sudo su -."""
        full_command = f"echo '{password}' | sudo -S su - {switch_user} -c '{command}'"
        return self.execute_command(full_command)

    def get_sftp(self):
        """Get an SFTPClient instance bound to this transport."""
        return paramiko.SFTPClient.from_transport(self._transport)

    def get_transport(self):
        """Get the raw Paramiko transport object."""
        return self._transport

    def close(self):
        """Close SSH connection and underlying socket."""
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


class SFTPClient:
    """SFTP Client wrapper for file upload/download operations."""

    def __init__(self, hostname: str, username: str, password: str, port: int = 22):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.port = port
        self.transport = None
        self.sftp = None

    def connect(self):
        """Establish SFTP connection with pre-tuned socket."""
        sock = socket.create_connection((self.hostname, self.port), timeout=30)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.transport = paramiko.Transport(sock)
        self.transport.default_window_size = 67108864
        self.transport.default_max_packet_size = 65536
        self.transport.connect(username=self.username, password=self.password)
        self.sftp = paramiko.SFTPClient.from_transport(self.transport)
        return self

    def mkdir_p(self, remote_path: str):
        """Create remote directory structure recursively."""
        dirs = remote_path.split('/')
        current_path = ''
        for d in dirs:
            if not d:
                continue
            current_path += '/' + d
            try:
                self.sftp.stat(current_path)
            except FileNotFoundError:
                self.sftp.mkdir(current_path)

    def upload(self, local_path: str, remote_path: str, progress_callback=None):
        """Upload file to SFTP server."""
        remote_dir = os.path.dirname(remote_path)
        self.mkdir_p(remote_dir)
        self.sftp.put(local_path, remote_path, callback=progress_callback, confirm=False)

    def download(self, remote_path: str, local_path: str, progress_callback=None, cancel_check=None):
        """Download file using single-pass prefetch SFTP download."""
        return fast_sftp_download(self.sftp, remote_path, local_path, progress_callback, cancel_check)

    def close(self):
        """Close SFTP session and underlying transport."""
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


def fast_sftp_download(sftp, remote_path: str, local_path: str, progress_callback=None, cancel_check=None):
    """High-speed SFTP download with read-ahead prefetching and single-pass MD5 computation.
    
    Returns (md5_hex_digest, file_size_bytes).
    """
    file_size = sftp.stat(remote_path).st_size
    md5_hash = hashlib.md5()
    transferred = 0
    chunk_size = 1048576  # 1 MB

    with sftp.open(remote_path, 'rb') as remote_file:
        try:
            remote_file.prefetch(file_size)
        except Exception:
            pass

        with open(local_path, 'wb') as local_file:
            while True:
                if cancel_check and cancel_check():
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


def fast_download(ssh_conn, sftp_conn, remote_path: str, local_path: str, progress_callback=None, cancel_check=None, use_scp: bool = True):
    """High-speed file download using SCP with fallback to pre-fetched SFTP.
    
    Returns (md5_hex_digest, file_size_bytes).
    """
    file_size = sftp_conn.stat(remote_path).st_size

    if use_scp:
        try:
            from scp import SCPClient

            def scp_cb(filename, size, sent):
                if cancel_check and cancel_check():
                    raise InterruptedError("Download cancelled by caller")
                if progress_callback:
                    progress_callback(sent, size)

            with SCPClient(ssh_conn.get_transport(), buff_size=65536, progress=scp_cb, socket_timeout=60.0) as scp:
                scp.get(remote_path, local_path)

            return calculate_local_md5(local_path), file_size

        except ImportError:
            pass
        except Exception as e:
            if isinstance(e, (InterruptedError, KeyboardInterrupt)) or (cancel_check and cancel_check()):
                raise

    return fast_sftp_download(sftp_conn, remote_path, local_path, progress_callback, cancel_check)
