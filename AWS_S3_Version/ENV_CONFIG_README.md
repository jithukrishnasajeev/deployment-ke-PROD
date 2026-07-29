# Environment Configuration

This project uses environment variables to securely store sensitive credentials like passwords.

## Setup

1. **Create your `.env` file:**
   - Copy the `.env.example` file to `.env`
   - Fill in your actual passwords in the `.env` file

   ```bash
   # On Windows (PowerShell)
   Copy-Item .env.example .env
   
   # On Linux/Mac
   cp .env.example .env
   ```

2. **Edit `.env` with your credentials:**
   ```env
   SOURCE_SERVER_PASSWORD=your_actual_password
   TARGET_SERVER_PASSWORD=your_actual_password
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements_deployment.txt
   ```

## Security Notes

- ✅ The `.env` file is ignored by git (see `.gitignore`)
- ✅ Passwords are loaded from environment variables at runtime
- ✅ No passwords are stored in the `deployment_config.json` files
- ⚠️ **NEVER** commit the `.env` file to version control
- ⚠️ Keep your `.env` file secure and restrict file permissions if needed

## How It Works

The application uses `python-dotenv` to load environment variables from the `.env` file:

1. On startup, the app reads the `.env` file
2. Passwords are loaded from `SOURCE_SERVER_PASSWORD` and `TARGET_SERVER_PASSWORD` environment variables
3. If environment variables are not set, the app falls back to values in `deployment_config.json` (which should be empty for security)

## Configuration Files

- `.env` - Your actual credentials (git ignored, not committed)
- `.env.example` - Template file (committed to git, no real passwords)
- `deployment_config.json` - Server configuration (no passwords)
- `.gitignore` - Ensures `.env` is never committed

## Deployment Configuration

All non-sensitive configuration is stored in `deployment_config.json`:
- Server hostnames and ports
- Usernames
- File paths
- WAR file mappings

Only passwords are stored in the `.env` file for security.
