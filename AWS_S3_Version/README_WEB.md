# iFlight Neo - Web Deployment Dashboard

Beautiful, modern web interface for managing iFlight Neo WAR deployments with real-time updates.

## Features

✨ **Beautiful UI**
- Modern, responsive design with gradient backgrounds
- Card-based layout with smooth animations
- Professional color scheme

🎯 **Customizable**
- Select individual WAR files to deploy
- Choose specific deployment steps (Step 1, Step 2, or both)
- Version number configuration
- Server management ready for expansion

📊 **Live Updates**
- Real-time progress tracking with WebSocket
- Live log streaming with color-coded messages
- File-by-file progress indication
- Completion counters

🚀 **Easy to Use**
- One-click deployment
- Select/Deselect all options
- Clear visual feedback
- Error handling with alerts

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements_deployment.txt
```

### 2. Start the Web Server

**Option A: Using batch file (Windows)**
```bash
start_web.bat
```

**Option B: Using Python directly**
```bash
python web_app.py
```

### 3. Open in Browser

Navigate to: **http://localhost:5000**

## Usage Guide

### Main Dashboard Layout

**Left Panel:**
- **Configuration**: Version and server details
- **WAR Files**: Select which files to deploy (individual selection)
- **Steps**: Choose Step 1 (Download) and/or Step 2 (Deploy)
- **Deploy Button**: Start the deployment

**Right Panel:**
- **Progress Bar**: Real-time progress with file counts
- **Live Logs**: Color-coded streaming logs
  - 🔵 Blue = Info messages
  - 🟢 Green = Success messages
  - 🟡 Yellow = Warning messages
  - 🔴 Red = Error messages

### Deployment Workflow

1. **Select Files**: Check the WAR files you want to deploy
   - Use "Select All" for full deployment
   - Individual selection for partial deployment

2. **Choose Steps**:
   - ✅ Step 1: Download from source server to local
   - ✅ Step 2: Upload from local to target server

3. **Set Version**: Enter or confirm the version number

4. **Deploy**: Click "Start Deployment" button

5. **Monitor**: Watch live progress and logs in real-time

### Individual Deployments

To deploy only specific modules:
1. Click "Deselect All"
2. Select only the modules you need (e.g., CREW_WEB, CREW_CWP)
3. Start deployment

## Configuration

Edit `deployment_config.json` to configure:
- Server credentials
- Paths
- WAR file mappings
- Default version

## Future Enhancements

The application is ready for:
- **Multi-Server Support**: Add more servers in the configuration panel
- **Scheduling**: Schedule deployments for specific times
- **History**: Track deployment history and rollbacks
- **User Management**: Add authentication and role-based access
- **Notifications**: Email/Slack notifications on completion
- **Parallel Deployments**: Deploy to multiple servers simultaneously

## Technical Stack

- **Backend**: Flask + Flask-SocketIO
- **Frontend**: Bootstrap 5 + Vanilla JavaScript
- **Real-time**: WebSocket (Socket.IO)
- **Icons**: Bootstrap Icons
- **Deployment**: Python paramiko for SSH/SFTP

## API Endpoints

- `GET /` - Main dashboard
- `GET /api/config` - Get current configuration
- `POST /api/config` - Update configuration
- `POST /api/deploy` - Start deployment
- `GET /api/status` - Get deployment status

## WebSocket Events

- `connect` - Client connected
- `log` - Log message (with timestamp, level, message)
- `progress` - Progress update (percentage, current file, counts)
- `deployment_complete` - Deployment finished

## Troubleshooting

**Port Already in Use:**
```bash
# Change port in web_app.py (last line):
socketio.run(app, host='0.0.0.0', port=5001, debug=True)
```

**Connection Issues:**
- Check firewall settings
- Verify Python and dependencies are installed
- Ensure deployment_config.json has correct credentials

**Live Updates Not Working:**
- Check browser console for WebSocket errors
- Verify Flask-SocketIO is installed correctly

## Security Notes

- The web server runs on localhost by default (0.0.0.0)
- For production use, add authentication
- Use HTTPS for remote connections
- Keep credentials secure in deployment_config.json

## License

Internal IBS Software tool for iFlight Neo deployments.
