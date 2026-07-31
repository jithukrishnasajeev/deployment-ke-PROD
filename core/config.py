"""
Dynamic Deployment Configuration Module.
Loads project, server, path, and AWS/SSH parameters from JSON files and environment variables.
"""

import os
import json
from dotenv import load_dotenv

# Ensure environment variables are loaded
load_dotenv()


class DeploymentConfig:
    """Enterprise Deployment Configuration container."""

    def __init__(self, config_path: str = None):
        # Default metadata
        self.APP_TITLE = "Deployment Automation System"
        self.VERSION = os.getenv('DEPLOYMENT_VERSION', '1.0.0')

        # Download Source ("ssh" or "s3")
        self.DOWNLOAD_SOURCE = os.getenv('DOWNLOAD_SOURCE', 'ssh').lower()

        # Source Server Settings (SSH Source)
        self.SOURCE_SERVER = os.getenv('SOURCE_SERVER_HOST', '127.0.0.1')
        self.SOURCE_USER = os.getenv('SOURCE_SERVER_USER', 'deploy')
        self.SOURCE_PORT = int(os.getenv('SOURCE_SERVER_PORT', '22'))
        self.SOURCE_SWITCH_USER = os.getenv('SOURCE_SWITCH_USER', '')
        self.SOURCE_PASSWORD = os.getenv('SOURCE_SERVER_PASSWORD', '')

        # Build paths & package details
        self.SOURCE_BASE_PATH = "/opt/deployments/builds"
        self.SOURCE_PATH = f"{self.SOURCE_BASE_PATH}/{self.VERSION}/"
        self.TAR_FILE = "Wars.tar"

        # Local download path
        self.LOCAL_DOWNLOAD_PATH = os.getenv('LOCAL_DOWNLOAD_PATH', os.path.join(os.getcwd(), "downloads"))

        # SFTP intermediary server settings (optional)
        self.SFTP_SERVER = os.getenv('SFTP_SERVER', '')
        self.SFTP_USER = os.getenv('SFTP_USER', '')
        self.SFTP_REMOTE_PATH = f"Releases/{self.VERSION}"

        # Target Server Settings
        self.TARGET_SERVER = os.getenv('TARGET_SERVER_HOST', '127.0.0.1')
        self.TARGET_USER = os.getenv('TARGET_SERVER_USER', 'deploy')
        self.TARGET_PORT = int(os.getenv('TARGET_SERVER_PORT', '22'))
        self.TARGET_PASSWORD = os.getenv('TARGET_SERVER_PASSWORD', '')
        self.TARGET_ROUTES = []

        # Target paths
        self.TARGET_UTILITIES_BASE = "/opt/deployments/utilities"
        self.TARGET_EXTRACT_PATH = f"{self.TARGET_UTILITIES_BASE}/{self.VERSION}/Wars"
        self.TARGET_DEPLOY_BASE = "/opt/deployments/applications"

        # AWS S3 Settings
        self.S3_BUCKET = os.getenv('S3_BUCKET', '')
        self.S3_PREFIX_TEMPLATE = "Releases/{version}/Wars/"
        self.S3_PROFILE = os.getenv('S3_PROFILE', 'default')
        self.S3_REGION = os.getenv('AWS_REGION', 'ap-south-1')

        # Transfer & Optimization options
        self.USE_SCP = True
        self.PARALLEL_DOWNLOADS = True
        self.MAX_THREADS = 4
        self.DIRECT_WAR_DOWNLOAD = True

        # Mapping of artifact filename prefix -> target application deployment sub-folder name
        # Example: [("app-web", "WEB"), ("app-api", "API")]
        self.WAR_MAPPINGS = []

        # Load from JSON if provided or default exists
        target_path = config_path or os.getenv('DEPLOYMENT_CONFIG_PATH', 'deployment_config.json')
        if os.path.exists(target_path):
            self.load_from_json(target_path)

    def load_from_json(self, config_path: str):
        """Parse configuration settings from a JSON file."""
        if not os.path.exists(config_path):
            return

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            print(f"[ERROR] Failed to load config from '{config_path}': {e}")
            return

        self.APP_TITLE = data.get('app_title', self.APP_TITLE)
        self.VERSION = data.get('version', self.VERSION)
        self.DOWNLOAD_SOURCE = data.get('download_source', self.DOWNLOAD_SOURCE).lower()

        # Source Server
        source = data.get('source_server', {})
        self.SOURCE_SERVER = source.get('host', self.SOURCE_SERVER)
        self.SOURCE_USER = source.get('username', self.SOURCE_USER)
        self.SOURCE_PORT = int(source.get('port', self.SOURCE_PORT))
        self.SOURCE_SWITCH_USER = source.get('switch_user', self.SOURCE_SWITCH_USER)
        self.SOURCE_PASSWORD = os.getenv('SOURCE_SERVER_PASSWORD', source.get('password', self.SOURCE_PASSWORD))

        # Local Path
        local = data.get('local', {})
        self.LOCAL_DOWNLOAD_PATH = local.get('download_path', self.LOCAL_DOWNLOAD_PATH)

        # Target Server Routes
        target = data.get('target_server', {})
        self.TARGET_PORT = int(target.get('port', self.TARGET_PORT))
        routes = target.get('routes', [])
        if routes:
            self.TARGET_ROUTES = routes
            self.TARGET_SERVER = routes[0].get('host', self.TARGET_SERVER)
            self.TARGET_USER = routes[0].get('username', self.TARGET_USER)
        else:
            self.TARGET_SERVER = target.get('host', self.TARGET_SERVER)
            self.TARGET_USER = target.get('username', self.TARGET_USER)
            self.TARGET_ROUTES = [{'host': self.TARGET_SERVER, 'username': self.TARGET_USER}]

        self.TARGET_PASSWORD = os.getenv('TARGET_SERVER_PASSWORD', target.get('password', self.TARGET_PASSWORD))

        # Paths
        paths = data.get('paths', {})
        self.SOURCE_BASE_PATH = paths.get('source_base', self.SOURCE_BASE_PATH)
        self.SOURCE_PATH = f"{self.SOURCE_BASE_PATH}/{self.VERSION}/" if not self.SOURCE_BASE_PATH.endswith('/') else f"{self.SOURCE_BASE_PATH}{self.VERSION}/"
        
        self.TARGET_UTILITIES_BASE = paths.get('target_utilities', self.TARGET_UTILITIES_BASE)
        self.TARGET_EXTRACT_PATH = f"{self.TARGET_UTILITIES_BASE}/{self.VERSION}/Wars"
        self.TARGET_DEPLOY_BASE = paths.get('target_deploy_base', self.TARGET_DEPLOY_BASE)

        # AWS S3 Settings
        s3 = data.get('s3_config', {})
        self.S3_BUCKET = s3.get('bucket', self.S3_BUCKET)
        self.S3_PREFIX_TEMPLATE = s3.get('prefix_template', self.S3_PREFIX_TEMPLATE)
        self.S3_PROFILE = s3.get('profile', self.S3_PROFILE)
        self.S3_REGION = s3.get('region', self.S3_REGION)

        # Transfer Optimizations
        transfer = data.get('transfer_optimization', {})
        self.USE_SCP = transfer.get('protocol', 'SCP').upper() == 'SCP' and transfer.get('enabled', True)
        self.PARALLEL_DOWNLOADS = transfer.get('parallel_downloads', self.PARALLEL_DOWNLOADS)
        self.MAX_THREADS = int(transfer.get('max_threads', self.MAX_THREADS))
        self.DIRECT_WAR_DOWNLOAD = transfer.get('direct_war_download', self.DIRECT_WAR_DOWNLOAD)

        # WAR Mappings
        war_mappings = data.get('war_mappings', {})
        if isinstance(war_mappings, dict):
            self.WAR_MAPPINGS = [(k, v) for k, v in war_mappings.items()]
        elif isinstance(war_mappings, list):
            self.WAR_MAPPINGS = war_mappings

    def to_dict(self) -> dict:
        """Export current configuration as a standard dictionary."""
        return {
            "app_title": self.APP_TITLE,
            "version": self.VERSION,
            "download_source": self.DOWNLOAD_SOURCE,
            "source_server": {
                "host": self.SOURCE_SERVER,
                "username": self.SOURCE_USER,
                "port": self.SOURCE_PORT,
                "switch_user": self.SOURCE_SWITCH_USER
            },
            "local": {
                "download_path": self.LOCAL_DOWNLOAD_PATH
            },
            "target_server": {
                "routes": self.TARGET_ROUTES if self.TARGET_ROUTES else [{"host": self.TARGET_SERVER, "username": self.TARGET_USER}],
                "port": self.TARGET_PORT
            },
            "paths": {
                "source_base": self.SOURCE_BASE_PATH,
                "target_utilities": self.TARGET_UTILITIES_BASE,
                "target_deploy_base": self.TARGET_DEPLOY_BASE
            },
            "s3_config": {
                "bucket": self.S3_BUCKET,
                "prefix_template": self.S3_PREFIX_TEMPLATE,
                "profile": self.S3_PROFILE,
                "region": self.S3_REGION
            },
            "transfer_optimization": {
                "enabled": True,
                "protocol": "SCP" if self.USE_SCP else "SFTP",
                "parallel_downloads": self.PARALLEL_DOWNLOADS,
                "max_threads": self.MAX_THREADS,
                "direct_war_download": self.DIRECT_WAR_DOWNLOAD
            },
            "war_mappings": dict(self.WAR_MAPPINGS)
        }


def load_config_from_json(config_path: str = 'deployment_config.json') -> DeploymentConfig:
    """Helper function to return a DeploymentConfig loaded from a JSON file."""
    return DeploymentConfig(config_path)
