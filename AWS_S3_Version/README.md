# 🚀 iFlight Neo Wars Deployment Automation System

A high-performance deployment automation suite and web dashboard for packaging, transferring, and deploying iFlight Neo `.war` application releases. 

Supports dual download sources (**SSH Source Server** and **AWS S3 Cloud Storage**), real-time Server-Sent Events (SSE) progress tracking, and multi-route PAM load distribution.

---

## 📋 System Requirements

- **Operating System**: Windows 10/11 / Windows Server
- **Python Runtime**: Python 3.8+ 
- **AWS CLI**: AWS CLI v2 installed ([Download AWS CLI v2](https://awscli.amazonaws.com/AWSCLIV2.msi))
- **Network / Access**:
  - Access to AWS S3 bucket `iflightdevrdits3` (via AWS SSO)
  - Network reachability to target server PAM nodes (`10.175.1.247`, `10.175.1.140`)

---

## ⚡ Quick Setup (Automated)

Run the included automatic setup script:

1. Double-click **`quick_setup.bat`** (or run `.\quick_setup.bat` in PowerShell/CMD).
2. The setup script will automatically:
   - Verify Python installation.
   - Install required dependencies (`flask`, `python-dotenv`, `paramiko`, `scp`).
   - Create/Update `%USERPROFILE%\.aws\config` with the required **`iFlightCrew_Dev`** AWS SSO profile.
   - Generate default `.env` configuration file if missing.
   - Launch the Web Dashboard at **`http://localhost:5000`**.

---

## 🔑 AWS S3 & SSO Configuration Details

### S3 Bucket & Profile
- **S3 Bucket**: `iflightdevrdits3`
- **S3 Release Path**: `s3://iflightdevrdits3/iFlight_Release/<version>/Wars/`
- **AWS Profile**: `iFlightCrew_Dev`
- **AWS Region**: `ap-south-1` (Mumbai)

### Automatic `.aws/config` Entry
The setup script adds the following profile block to your local `~/.aws/config`:

```ini
[profile iFlightCrew_Dev]
sso_session = ibs-sso
sso_account_id = 680323250112
sso_role_name = IT_DEV_AWS_Access
region = ap-south-1
output = json

[sso-session ibs-sso]
sso_start_url = https://d-926715f2ed.awsapps.com/start
sso_region = ap-south-1
```

---

## ⚙️ Configuration Reference (`deployment_config.json`)

The application is controlled by `deployment_config.json`:

```json
{
    "version": "3.96.34.267",
    "download_source": "s3",
    "s3_config": {
        "bucket": "iflightdevrdits3",
        "prefix_template": "iFlight_Release/{version}/Wars/",
        "profile": "iFlightCrew_Dev",
        "region": "ap-south-1"
    },
    "source_server": {
        "host": "10.246.26.148",
        "username": "a-10266",
        "port": 22,
        "switch_user": "iflight_user"
    },
    "local": {
        "download_path": "D:\\RMT_TOOLS\\Deployment_KE\\downloads"
    },
    "target_server": {
        "routes": [
            {
                "host": "PAM_NV.ibsplc.aero",
                "username": "a-10266@ibsplc.com%iflightkeprod%10.175.1.247"
            },
            {
                "host": "PAM_TYO.ibsplc.aero",
                "username": "a-10266@ibsplc.com%iflightkeprod%10.175.1.140"
            }
        ],
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
        "parallel_downloads": true,
        "max_threads": 4,
        "direct_war_download": true
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
```

---

## 🔒 Credentials & Environment (`.env`)

Store remote server passwords securely in `.env`:

```ini
SOURCE_SERVER_PASSWORD=your_source_password_here
TARGET_SERVER_PASSWORD=your_target_password_here
```
*(Passwords can also be updated directly from the **System Configuration** tab on the Web Dashboard).*

---

## 🔄 Deployment Execution Steps

1. **Step 1: Download to Local**
   - Downloads `.war` files from chosen source (**SSH Server** or **AWS S3**) to your local `downloads` folder.
   - Calculates and verifies MD5 checksums with real-time percentage progress.
2. **Step 2: Upload & Extract to Target**
   - Uploads `.war` files to target server utilities directory (`/iflightneo/global/Utilities/<version>/Wars`).
   - Uses multi-thread SCP/SFTP transfer across distributed PAM routes.
3. **Step 3: Deploy to Crew Web Folders**
   - Deploys `.war` files into respective application folders (`CREW_CWP`, `CREW_DSM`, `CREW_WEB`, etc.).

---

## 📁 Repository Structure

```
deployment-ke/
│── quick_setup.bat             # Automatic environment setup & launcher
│── start_web.bat               # Launcher for Web Application
│── run_deployment.bat          # Launcher for CLI Interactive Deployment
│── web_app.py                  # Flask Web Server & SSE Real-time Progress Engine
│── deployment_automation.py    # Core SSH, SCP & S3 Deployment Engine
│── deployment_config.json      # Configuration settings (S3, Servers, Mappings)
│── requirements.txt            # Python package dependencies
│── templates/
│   └── index.html              # Modern Web Dashboard UI
│── static/
│   ├── css/style.css           # Glassmorphism dark mode styles & S3 badges
│   └── js/app.js               # Event-driven UI controller & Auto-SSO trigger
└── AWS_S3_Version/             # Complete standalone AWS S3 release package
```

---

## 🌐 Running Web Dashboard Manually

```powershell
# Activate environment & start server
python web_app.py
```
Open **`http://localhost:5000`** in your browser.
