"""
Utility functions for deployment engine (hashing, sizing, system commands).
"""

import os
import shutil
import hashlib


def format_size(size_bytes: int) -> str:
    """Format bytes into human readable string (B, KB, MB, GB, TB)."""
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    val = float(size_bytes)
    for unit in units:
        if val < 1024.0 or unit == units[-1]:
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} B"


def get_aws_cmd() -> str:
    """Locate AWS CLI executable on the local system without surrounding quotes."""
    aws_path = shutil.which('aws')
    if aws_path:
        return aws_path
    possible_paths = [
        r"C:\Program Files\Amazon\AWSCLIV2\aws.exe",
        r"C:\Program Files (x86)\AWS CLI\aws.exe",
        r"C:\Program Files\Amazon\AWS CLI\aws.exe",
        r"C:\aws-cli\aws.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\Amazon\AWSCLIV2\aws.exe"),
        os.path.expanduser(r"~\AppData\Local\AWSCLIV2\aws.exe"),
        "/usr/local/bin/aws",
        "/usr/bin/aws"
    ]
    for path in possible_paths:
        if os.path.exists(path):
            return path
    return 'aws'


def calculate_local_md5(file_path: str, chunk_size: int = 1048576) -> str:
    """Calculate MD5 checksum of a local file in 1MB chunks."""
    if not os.path.exists(file_path):
        return ""
    md5_hash = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()


def get_remote_md5(ssh_client, remote_path: str) -> str:
    """Get remote file MD5 checksum using md5sum or md5 command over SSH."""
    cmd = f"md5sum '{remote_path}' 2>/dev/null || md5 '{remote_path}' 2>/dev/null"
    output, _, exit_code = ssh_client.execute_command(cmd)
    if exit_code == 0 and output:
        parts = output.strip().split()
        if parts:
            return parts[0]
    return ""
