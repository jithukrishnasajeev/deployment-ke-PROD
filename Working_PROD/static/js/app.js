// Server-Sent Events connection
let eventSource = null;
let config = null;
let isDeploying = false;
let fileSizes = {};

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    loadConfiguration();
    connectSSE();
});

// Connect to Server-Sent Events
function connectSSE() {
    if (eventSource) {
        eventSource.close();
    }
    
    eventSource = new EventSource('/api/events');
    
    eventSource.onopen = function() {
        console.log('SSE Connected');
        updateStatus('Connected', 'bg-success');
    };
    
    eventSource.onerror = function(error) {
        console.error('SSE Error:', error);
        updateStatus('Reconnecting...', 'bg-warning');
        // Auto-reconnect is handled by browser
    };
    
    eventSource.onmessage = function(event) {
        try {
            const message = JSON.parse(event.data);
            handleMessage(message);
        } catch (e) {
            console.error('Failed to parse SSE message:', e);
        }
    };
}

// Handle incoming SSE messages
function handleMessage(message) {
    switch (message.type) {
        case 'log':
            addLog(message.data.level, message.data.message, message.data.timestamp);
            break;
        case 'progress':
            updateProgress(message.data.progress, message.data.current_file, 
                          message.data.completed, message.data.total);
            break;
        case 'file_size':
            updateFileSizes(message.data);
            break;
        case 'complete':
            handleDeploymentComplete(message.data);
            break;
        case 'ping':
            // Keepalive, ignore
            break;
        case 'status':
            // Initial status
            if (message.data.running) {
                isDeploying = true;
                updateStatus('Deploying...', 'bg-warning pulse');
                setButtonsDisabled(true);
            }
            break;
    }
}

// Load configuration from server
async function loadConfiguration() {
    try {
        const response = await fetch('/api/config');
        const data = await response.json();
        
        if (data.error) {
            addLog('error', 'Failed to load configuration: ' + data.error);
            return;
        }
        
        config = data;
        
        // Populate form fields - don't overwrite version if user has entered one
        const versionInput = document.getElementById('version-input');
        if (!versionInput.value || versionInput.value === versionInput.placeholder) {
            versionInput.value = data.version;
        }
        
        document.getElementById('source-server').value = data.source_server;
        document.getElementById('target-server').value = data.target_server;
        document.getElementById('target-username').value = data.target_username || '';
        document.getElementById('local-path').value = data.local_path;
        
        // Load WAR files list
        loadWarFilesList(data.war_files);
        
        // Add version change listener to highlight when modified
        versionInput.addEventListener('input', function() {
            if (this.value !== data.version) {
                this.style.borderColor = '#fbbf24';
                this.style.boxShadow = '0 0 0 3px rgba(251, 191, 36, 0.2)';
            } else {
                this.style.borderColor = '';
                this.style.boxShadow = '';
            }
        });
        
        // Add change listeners for target fields to highlight when modified
        const targetServerInput = document.getElementById('target-server');
        const targetUsernameInput = document.getElementById('target-username');
        
        targetServerInput.addEventListener('input', function() {
            if (this.value !== data.target_server) {
                this.style.borderColor = '#fbbf24';
                this.style.boxShadow = '0 0 0 3px rgba(251, 191, 36, 0.2)';
            } else {
                this.style.borderColor = '';
                this.style.boxShadow = '';
            }
        });
        
        targetUsernameInput.addEventListener('input', function() {
            if (this.value !== (data.target_username || '')) {
                this.style.borderColor = '#fbbf24';
                this.style.boxShadow = '0 0 0 3px rgba(251, 191, 36, 0.2)';
            } else {
                this.style.borderColor = '';
                this.style.boxShadow = '';
            }
        });
        
    } catch (error) {
        console.error('Error loading configuration:', error);
        addLog('error', 'Failed to load configuration: ' + error.message);
    }
}

// Load WAR files checkboxes
function loadWarFilesList(warFiles) {
    const container = document.getElementById('war-files-list');
    container.innerHTML = '';
    
    warFiles.forEach((warFile, index) => {
        const warName = warFile.replace('iflight-', '').replace('-webapp', '').toUpperCase();
        const div = document.createElement('div');
        div.className = 'war-item';
        div.innerHTML = `
            <input type="checkbox" id="war-${index}" value="${warFile}" checked>
            <label for="war-${index}">${warName}</label>
        `;
        container.appendChild(div);
    });
}

// Select/Deselect all WAR files
function selectAll() {
    document.querySelectorAll('.war-item input[type="checkbox"]').forEach(cb => cb.checked = true);
}

function deselectAll() {
    document.querySelectorAll('.war-item input[type="checkbox"]').forEach(cb => cb.checked = false);
}

// Run individual step
async function runStep(stepNumber) {
    if (isDeploying) {
        alert('Deployment already in progress!');
        return;
    }
    
    const selectedWars = getSelectedWars();
    if (selectedWars.length === 0) {
        alert('Please select at least one WAR file.');
        return;
    }
    
    const version = document.getElementById('version-input').value.trim();
    if (!version) {
        alert('Please enter a version number.');
        return;
    }
    
    const stepNames = { 1: 'Download from Source', 2: 'Upload and Deploy' };
    if (!confirm(`Run Step ${stepNumber}: ${stepNames[stepNumber]}\n\n${selectedWars.length} WAR file(s) for version ${version}?`)) {
        return;
    }
    
    await executeDeployment(selectedWars, [stepNumber], version);
}

// Start full deployment (both steps)
async function startDeployment() {
    if (isDeploying) {
        alert('Deployment already in progress!');
        return;
    }
    
    const selectedWars = getSelectedWars();
    if (selectedWars.length === 0) {
        alert('Please select at least one WAR file to deploy.');
        return;
    }
    
    const version = document.getElementById('version-input').value.trim();
    if (!version) {
        alert('Please enter a version number.');
        return;
    }
    
    if (!confirm(`Run FULL deployment of ${selectedWars.length} WAR file(s) for version ${version}?\n\nThis will run both Step 1 (Download) and Step 2 (Upload & Deploy).`)) {
        return;
    }
    
    await executeDeployment(selectedWars, [1, 2], version);
}

// Get selected WAR files
function getSelectedWars() {
    const selectedWars = [];
    document.querySelectorAll('.war-item input[type="checkbox"]:checked').forEach(cb => {
        selectedWars.push(cb.value);
    });
    return selectedWars;
}

// Execute deployment with given parameters
async function executeDeployment(selectedWars, steps, version) {
    const bypassNetwork = document.getElementById('bypass-network').checked;
    const targetServer = document.getElementById('target-server').value.trim();
    const targetUsername = document.getElementById('target-username').value.trim();
    
    // Validate required fields
    if (!targetServer) {
        alert('Please enter target server hostname.');
        return;
    }
    if (!targetUsername) {
        alert('Please enter target username.');
        return;
    }
    
    // If Step 2 is included and network bypass is not checked, verify target connectivity
    if (steps.includes(2) && !bypassNetwork) {
        const proceedWithTarget = await verifyTargetConnection();
        if (!proceedWithTarget) {
            return;
        }
    }
    
    clearLogs();
    
    // Activate progress bar animation
    const progressSection = document.querySelector('.progress-section');
    if (progressSection) {
        progressSection.classList.add('active');
    }
    
    // Log deployment configuration
    addLog('info', '═══════════════════════════════════════════════════════════');
    addLog('info', `🚀 Starting Deployment - Version: ${version}`);
    addLog('info', `📦 WAR Files: ${selectedWars.length} selected`);
    addLog('info', `📋 Steps: ${steps.length === 1 ? `Step ${steps[0]} only` : 'Full deployment (Step 1 + Step 2)'}`);
    addLog('info', `🎯 Target: ${targetServer} (${targetUsername})`);
    addLog('info', '═══════════════════════════════════════════════════════════');
    
    try {
        const response = await fetch('/api/deploy', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                selected_wars: selectedWars,
                steps: steps,
                version: version,
                target_server: targetServer,
                target_username: targetUsername,
                bypass_network: bypassNetwork
            })
        });
        
        const data = await response.json();
        
        if (data.error) {
            alert('Failed to start deployment: ' + data.error);
            return;
        }
        
        isDeploying = true;
        const stepText = steps.length === 1 ? `Step ${steps[0]}` : 'Full Deploy';
        updateStatus(`${stepText}...`, 'bg-warning pulse');
        setButtonsDisabled(true);
        
        // Add deploying animation to button
        document.getElementById('deploy-btn').classList.add('deploying');
        
    } catch (error) {
        console.error('Error starting deployment:', error);
        alert('Failed to start deployment: ' + error.message);
    }
}

// Verify target server connection before Step 2
async function verifyTargetConnection() {
    // First check if we have recent connection test results
    const now = Date.now();
    const lastTest = sessionStorage.getItem('lastConnectionTest');
    const testResults = sessionStorage.getItem('connectionTestResults');
    
    if (lastTest && testResults && (now - parseInt(lastTest)) < 300000) { // 5 minutes
        const results = JSON.parse(testResults);
        if (results.target === 'success') {
            return true; // Target was working recently
        }
        
        if (results.target !== 'success') {
            const proceed = confirm(
                'Target server (10.175.1.247) was unreachable in recent tests.\n\n' +
                'This could be due to:\n' +
                '• Not connected to company VPN\n' +
                '• Network/firewall restrictions\n' +
                '• Server maintenance\n\n' +
                'Do you want to try deployment anyway?\n' +
                '(Source server may have access to target)'
            );
            return proceed;
        }
    }
    
    return true; // No recent test data, allow deployment
}

// Test server connection
async function checkServerConnection() {
    const version = document.getElementById('version-input').value.trim();
    const targetServer = document.getElementById('target-server').value.trim();
    const targetUsername = document.getElementById('target-username').value.trim();
    
    if (!version) {
        alert('Please enter a version number first.');
        return;
    }
    if (!targetServer) {
        alert('Please enter target server hostname.');
        return;
    }
    if (!targetUsername) {
        alert('Please enter target username.');
        return;
    }
    
    addLog('info', '═══════════════════════════════════════════════════════════');
    addLog('info', `🔍 Testing connections for version: ${version}`);
    addLog('info', `🎯 Target: ${targetServer} (${targetUsername})`);
    addLog('info', '═══════════════════════════════════════════════════════════');
    updateStatus('Testing...', 'bg-warning pulse');
    
    try {
        const response = await fetch('/api/test-connection', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                version: version,
                target_server: targetServer,
                target_username: targetUsername
            })
        });
        const data = await response.json();
        
        // Store test results for later use
        sessionStorage.setItem('lastConnectionTest', Date.now().toString());
        sessionStorage.setItem('connectionTestResults', JSON.stringify(data));
        
        // Update connection status indicators
        updateConnectionStatus(data.source, data.target);
        
        if (data.success) {
            addLog('success', '✅ All server connections successful!');
            updateStatus('Connected', 'bg-success');
        } else {
            if (data.source === 'success' && data.target !== 'success') {
                addLog('warning', '⚠️ Source OK, Target unreachable');
                addLog('info', 'You can still run Step 1 (Download) or enable bypass for Step 2');
                updateStatus('Partial', 'bg-warning');
            } else {
                addLog('error', '❌ Connection test failed');
                updateStatus('Failed', 'bg-danger');
            }
        }
        
        // Update bypass checkbox hint
        const bypassCheckbox = document.getElementById('bypass-network');
        if (data.target !== 'success') {
            bypassCheckbox.checked = true;
            bypassCheckbox.parentElement.style.display = 'block';
            addLog('info', '💡 Network bypass enabled automatically');
        } else {
            bypassCheckbox.checked = false;
        }
        
    } catch (error) {
        addLog('error', '❌ Connection test failed: ' + error.message);
        updateStatus('Error', 'bg-danger');
        sessionStorage.removeItem('lastConnectionTest');
        sessionStorage.removeItem('connectionTestResults');
    }
}

// Cancel deployment
async function cancelDeployment() {
    if (!isDeploying) {
        addLog('info', 'No deployment in progress.');
        return;
    }
    
    if (!confirm('Are you sure you want to cancel the deployment?')) {
        return;
    }
    
    try {
        await fetch('/api/cancel', { method: 'POST' });
        addLog('warning', 'Cancellation requested...');
    } catch (error) {
        addLog('error', 'Failed to cancel: ' + error.message);
    }
}

// Enable/disable all deployment buttons
function setButtonsDisabled(disabled) {
    document.getElementById('deploy-btn').disabled = disabled;
    document.getElementById('step1-btn').disabled = disabled;
    document.getElementById('step2-btn').disabled = disabled;
}

// Handle deployment completion
function handleDeploymentComplete(data) {
    isDeploying = false;
    setButtonsDisabled(false);
    
    const progressSection = document.querySelector('.progress-section');
    const deployBtn = document.getElementById('deploy-btn');
    
    deployBtn.classList.remove('deploying');
    
    if (data.cancelled) {
        updateStatus('Cancelled', 'bg-warning');
        if (progressSection) {
            progressSection.classList.remove('active');
        }
    } else {
        updateStatus('Completed', 'bg-success');
        if (progressSection) {
            progressSection.classList.remove('active');
            progressSection.classList.add('progress-complete');
        }
        // Add celebration effect
        createConfetti();
    }
}

// Create confetti celebration effect
function createConfetti() {
    const colors = ['#6366f1', '#8b5cf6', '#06b6d4', '#10b981', '#f59e0b', '#ec4899'];
    const container = document.querySelector('.progress-section');
    if (!container) return;
    
    for (let i = 0; i < 30; i++) {
        const confetti = document.createElement('div');
        confetti.style.cssText = `
            position: absolute;
            width: 8px;
            height: 8px;
            background: ${colors[Math.floor(Math.random() * colors.length)]};
            border-radius: 50%;
            top: 50%;
            left: ${Math.random() * 100}%;
            animation: confettiFall ${1 + Math.random()}s ease-out forwards;
            z-index: 100;
        `;
        container.appendChild(confetti);
        setTimeout(() => confetti.remove(), 1500);
    }
}

// Update file sizes table
function updateFileSizes(sizes) {
    fileSizes = sizes;
    const tbody = document.getElementById('file-sizes-body');
    
    const rows = Object.entries(sizes).map(([prefix, info]) => {
        const statusBadge = getStatusBadge(info.status);
        const sourceSize = info.source_size > 0 ? formatSize(info.source_size) : '-';
        const targetSize = info.target_size > 0 ? formatSize(info.target_size) : '-';
        
        return `
            <tr>
                <td><strong>${info.name || prefix}</strong></td>
                <td class="text-end file-size-source">${sourceSize}</td>
                <td class="text-end file-size-target">${targetSize}</td>
                <td class="text-center">${statusBadge}</td>
            </tr>
        `;
    }).join('');
    
    tbody.innerHTML = rows || '<tr class="empty-row"><td colspan="4"><i class="bi bi-inbox"></i><span>Run deployment to see sizes</span></td></tr>';
}

// Get status badge HTML
function getStatusBadge(status) {
    const badges = {
        'pending': '<span class="status-badge status-pending">Pending</span>',
        'processing': '<span class="status-badge status-downloading">Processing</span>',
        'downloading': '<span class="status-badge status-downloading">Downloading</span>',
        'downloaded': '<span class="status-badge status-downloaded">Downloaded</span>',
        'uploading': '<span class="status-badge status-uploading">Uploading</span>',
        'deployed': '<span class="status-badge status-deployed">Deployed</span>',
        'error': '<span class="status-badge status-error">Error</span>',
        'warning': '<span class="status-badge status-pending">Warning</span>'
    };
    return badges[status] || badges['pending'];
}

// Update connection status indicators with animations
function updateConnectionStatus(source, target) {
    const sourceEl = document.getElementById('source-status');
    const targetEl = document.getElementById('target-status');
    
    if (source !== undefined) {
        const sourceStatus = sourceEl.querySelector('.conn-status');
        sourceEl.classList.remove('success', 'error', 'testing');
        sourceStatus.classList.remove('success', 'error', 'unknown');
        
        // Add animation
        sourceEl.classList.add('scale-in');
        setTimeout(() => sourceEl.classList.remove('scale-in'), 400);
        
        if (source === 'success') {
            sourceEl.classList.add('success');
            sourceStatus.classList.add('success');
            sourceStatus.textContent = 'Connected';
            // Success bounce
            sourceEl.querySelector('.conn-icon').classList.add('success-bounce');
            setTimeout(() => sourceEl.querySelector('.conn-icon').classList.remove('success-bounce'), 600);
        } else if (source) {
            sourceEl.classList.add('error');
            sourceStatus.classList.add('error');
            sourceStatus.textContent = 'Failed';
            // Error shake
            sourceEl.classList.add('error-shake');
            setTimeout(() => sourceEl.classList.remove('error-shake'), 600);
        }
    }
    
    if (target !== undefined) {
        const targetStatus = targetEl.querySelector('.conn-status');
        targetEl.classList.remove('success', 'error', 'testing');
        targetStatus.classList.remove('success', 'error', 'unknown');
        
        // Add animation
        targetEl.classList.add('scale-in');
        setTimeout(() => targetEl.classList.remove('scale-in'), 400);
        
        if (target === 'success') {
            targetEl.classList.add('success');
            targetStatus.classList.add('success');
            targetStatus.textContent = 'Connected';
            // Success bounce
            targetEl.querySelector('.conn-icon').classList.add('success-bounce');
            setTimeout(() => targetEl.querySelector('.conn-icon').classList.remove('success-bounce'), 600);
        } else if (target) {
            targetEl.classList.add('error');
            targetStatus.classList.add('error');
            targetStatus.textContent = 'Failed';
            // Error shake  
            targetEl.classList.add('error-shake');
            setTimeout(() => targetEl.classList.remove('error-shake'), 600);
        }
    }
}

// Format file size
function formatSize(bytes) {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let size = bytes;
    let unitIndex = 0;
    while (size >= 1024 && unitIndex < units.length - 1) {
        size /= 1024;
        unitIndex++;
    }
    return `${size.toFixed(2)} ${units[unitIndex]}`;
}

// Update status badge
function updateStatus(text, badgeClass) {
    const statusBadge = document.getElementById('status-badge');
    const statusText = statusBadge.querySelector('.status-text');
    const statusDot = statusBadge.querySelector('.status-dot');
    
    statusText.textContent = text;
    
    // Add change animation
    statusBadge.classList.add('changed');
    setTimeout(() => statusBadge.classList.remove('changed'), 500);
    
    // Update status indicator class
    statusBadge.classList.remove('warning', 'error', 'success', 'deploying');
    if (badgeClass.includes('warning')) {
        statusBadge.classList.add('warning');
    } else if (badgeClass.includes('danger') || badgeClass.includes('error')) {
        statusBadge.classList.add('error');
    } else if (badgeClass.includes('success')) {
        statusBadge.classList.add('success');
    }
    
    // Add deploying animation when applicable
    if (text.toLowerCase().includes('deploy')) {
        statusBadge.classList.add('deploying');
    }
}

// Add log entry with enhanced animations
function addLog(level, message, timestamp) {
    const logsContainer = document.getElementById('logs-container');
    const logEntry = document.createElement('div');
    logEntry.className = 'log-entry log-' + level + ' new';
    
    // Remove new class after animation
    setTimeout(() => logEntry.classList.remove('new'), 1000);
    
    const time = timestamp || new Date().toLocaleTimeString();
    
    logEntry.innerHTML = `
        <span class="log-time">${time}</span>
        <span class="log-message">${escapeHtml(message)}</span>
    `;
    
    logsContainer.appendChild(logEntry);
    logsContainer.scrollTop = logsContainer.scrollHeight;
}

// Update progress bar with enhanced animations
function updateProgress(progress, currentFile, completed, total) {
    const progressBar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');
    const currentFileSpan = document.getElementById('current-file');
    const completedSpan = document.getElementById('completed-count');
    const totalSpan = document.getElementById('total-count');
    const progressSection = document.querySelector('.progress-section');
    
    const roundedProgress = Math.round(progress);
    const oldProgress = parseInt(progressBar.style.width) || 0;
    
    progressBar.style.width = roundedProgress + '%';
    progressText.textContent = roundedProgress + '%';
    
    // Add/remove active class for animation
    if (progressSection) {
        if (roundedProgress > 0 && roundedProgress < 100) {
            progressSection.classList.add('active');
            progressSection.classList.remove('progress-complete', 'complete');
        } else if (roundedProgress >= 100) {
            progressSection.classList.remove('active');
            progressSection.classList.add('progress-complete', 'complete');
        } else {
            progressSection.classList.remove('active', 'progress-complete', 'complete');
        }
    }
    
    if (currentFile) {
        // Animate file name change
        if (currentFileSpan.textContent !== currentFile) {
            currentFileSpan.style.opacity = '0';
            setTimeout(() => {
                currentFileSpan.textContent = currentFile;
                currentFileSpan.style.opacity = '1';
            }, 150);
        }
        currentFileSpan.classList.add('file-processing');
    }
    
    if (completed !== undefined && total !== undefined) {
        // Animate counter change
        if (parseInt(completedSpan.textContent) !== completed) {
            completedSpan.classList.add('counter');
        }
        completedSpan.textContent = completed;
        totalSpan.textContent = total;
    }
}

// Clear logs
function clearLogs() {
    document.getElementById('logs-container').innerHTML = '';
    // Reset file sizes table
    document.getElementById('file-sizes-body').innerHTML = `
        <tr class="empty-row">
            <td colspan="4">
                <i class="bi bi-inbox"></i>
                <span>Run deployment to see sizes</span>
            </td>
        </tr>
    `;
    fileSizes = {};
    
    // Reset progress state
    const progressBar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');
    const progressSection = document.querySelector('.progress-section');
    const currentFileSpan = document.getElementById('current-file');
    const completedSpan = document.getElementById('completed-count');
    const totalSpan = document.getElementById('total-count');
    
    progressBar.style.width = '0%';
    progressText.textContent = '0%';
    currentFileSpan.textContent = '-';
    currentFileSpan.classList.remove('file-processing');
    completedSpan.textContent = '0';
    totalSpan.textContent = '0';
    
    if (progressSection) {
        progressSection.classList.remove('active', 'progress-complete');
    }
}

// Utility: Escape HTML
function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
}

/* ================================================
   COLUMN RESIZE FUNCTIONALITY
   ================================================ */
(function initColumnResize() {
    const container = document.querySelector('.main-container');
    const handles = document.querySelectorAll('.resize-handle');
    
    // Disable resize on tablets and below
    function isResizeEnabled() {
        return window.innerWidth > 1024;
    }
    
    // Load saved widths from localStorage
    function loadSavedWidths() {
        if (!isResizeEnabled()) return;
        
        const savedLeftWidth = localStorage.getItem('col-left-width');
        const savedRightWidth = localStorage.getItem('col-right-width');
        
        if (savedLeftWidth) {
            container.style.setProperty('--col-left-width', savedLeftWidth + 'px');
        }
        if (savedRightWidth) {
            container.style.setProperty('--col-right-width', savedRightWidth + 'px');
        }
    }
    
    loadSavedWidths();
    
    // Re-apply saved widths on window resize if screen becomes large enough
    window.addEventListener('resize', function() {
        if (isResizeEnabled()) {
            loadSavedWidths();
        }
    });
    
    handles.forEach(handle => {
        let isResizing = false;
        let startX = 0;
        let startWidth = 0;
        let targetColumn = null;
        const direction = handle.dataset.direction; // 'left' or 'right'
        
        handle.addEventListener('mousedown', function(e) {
            // Don't allow resize on small screens
            if (!isResizeEnabled()) return;
            
            e.preventDefault();
            isResizing = true;
            startX = e.pageX;
            handle.classList.add('resizing');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            
            // Get the column to resize
            if (direction === 'left') {
                targetColumn = document.querySelector('[data-column="left"]');
            } else {
                targetColumn = document.querySelector('[data-column="right"]');
            }
            
            startWidth = targetColumn.offsetWidth;
            
            // Add event listeners to document for better tracking
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
        
        function onMouseMove(e) {
            if (!isResizing) return;
            
            const deltaX = direction === 'left' ? (e.pageX - startX) : (startX - e.pageX);
            let newWidth = startWidth + deltaX;
            
            // Set min/max constraints
            const minWidth = 180;
            const maxWidth = direction === 'left' ? 500 : 600;
            
            newWidth = Math.max(minWidth, Math.min(maxWidth, newWidth));
            
            // Update the CSS variable
            if (direction === 'left') {
                container.style.setProperty('--col-left-width', newWidth + 'px');
            } else {
                container.style.setProperty('--col-right-width', newWidth + 'px');
            }
        }
        
        function onMouseUp() {
            if (!isResizing) return;
            
            isResizing = false;
            handle.classList.remove('resizing');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            
            // Save to localStorage
            if (direction === 'left') {
                const width = targetColumn.offsetWidth;
                localStorage.setItem('col-left-width', width);
            } else {
                const width = targetColumn.offsetWidth;
                localStorage.setItem('col-right-width', width);
            }
            
            // Remove event listeners
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
        }
    });
})();
