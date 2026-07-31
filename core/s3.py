"""
AWS S3 Integration Module.
Downloads build artifacts from AWS S3 buckets using AWS CLI or Boto3.
"""

import os
import subprocess
import threading
from .utils import get_aws_cmd, calculate_local_md5


class S3Client:
    """AWS S3 download client manager."""

    def __init__(self, bucket: str, profile: str = None, region: str = "ap-south-1"):
        self.bucket = bucket
        self.profile = profile
        self.region = region
        self.aws_cmd = get_aws_cmd()

    def download_file(self, s3_key: str, local_path: str, progress_callback=None, cancel_check=None) -> tuple:
        """Download a single S3 object to local file path.
        
        Returns (local_md5, file_size_bytes).
        """
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        s3_uri = f"s3://{self.bucket}/{s3_key.lstrip('/')}"

        cmd = [self.aws_cmd, 's3', 'cp', s3_uri, local_path]
        if self.profile:
            cmd.extend(['--profile', self.profile])
        if self.region:
            cmd.extend(['--region', self.region])

        # Execute aws s3 cp command
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=isinstance(self.aws_cmd, str) and ' ' in self.aws_cmd
        )

        while process.poll() is None:
            if cancel_check and cancel_check():
                process.terminate()
                raise InterruptedError("S3 download cancelled by caller")
            
            if os.path.exists(local_path) and progress_callback:
                size = os.path.getsize(local_path)
                progress_callback(size, size)
            
            subprocess.Popen.wait(process, timeout=0.5)

        stdout, stderr = process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"AWS S3 download failed ({process.returncode}): {stderr.strip()}")

        file_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
        md5 = calculate_local_md5(local_path) if file_size > 0 else ""
        return md5, file_size
