"""
Enterprise Deployment Automation System (CLI Entry Point & Core Facade).

This module re-exports the modular core package (`core/`) and provides the 
command-line execution interface for standalone pipeline runs.
"""

import sys
import os

from core.config import DeploymentConfig, load_config_from_json
from core.engine import DeploymentEngine
from core.ssh import SSHClient, SFTPClient, fast_sftp_download, fast_download
from core.s3 import S3Client
from core.utils import format_size, get_aws_cmd, calculate_local_md5, get_remote_md5

__all__ = [
    'DeploymentConfig',
    'DeploymentEngine',
    'SSHClient',
    'SFTPClient',
    'S3Client',
    'format_size',
    'get_aws_cmd',
    'calculate_local_md5',
    'get_remote_md5',
    'load_config_from_json',
    'fast_sftp_download',
    'fast_download'
]


def main():
    """Command-line execution runner."""
    print("=" * 60)
    print("Enterprise Deployment Automation Pipeline")
    print("=" * 60)

    config_path = os.getenv('DEPLOYMENT_CONFIG_PATH', 'deployment_config.json')
    config = load_config_from_json(config_path)

    print(f"[INFO] Version:         {config.VERSION}")
    print(f"[INFO] Download Source: {config.DOWNLOAD_SOURCE.upper()}")
    print(f"[INFO] Target Host:     {config.TARGET_SERVER}")
    print(f"[INFO] Config File:     {config_path}\n")

    engine = DeploymentEngine(config)
    success = engine.run_all()

    if success:
        print("\n[SUCCESS] All pipeline steps completed successfully!")
        sys.exit(0)
    else:
        print("\n[FAILED] One or more pipeline steps encountered errors.")
        sys.exit(1)


if __name__ == '__main__':
    main()
