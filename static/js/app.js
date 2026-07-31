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
            if (message.data.file_sizes) {
                updateFileSizes(message.data.file_sizes);
            }
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

let currentDownloadSource = 'ssh';

function setDownloadSource(source, isUserAction = false) {
    currentDownloadSource = source;
    const sshPill = document.getElementById('src-pill-ssh');
    const s3Pill = document.getElementById('src-pill-s3');
    const sshContainer = document.getElementById('source-ssh-container');
    const s3Container = document.getElementById('source-s3-container');

    if (source === 's3') {
        if (sshPill) sshPill.classList.remove('active');
        if (s3Pill) s3Pill.classList.add('active');
        if (sshContainer) sshContainer.style.display = 'none';
        if (s3Container) s3Container.style.display = 'block';
        
        if (isUserAction) {
            checkAndAutoLoginAwsSso();
        }
    } else {
        if (s3Pill) s3Pill.classList.remove('active');
        if (sshPill) sshPill.classList.add('active');
        if (s3Container) s3Container.style.display = 'none';
        if (sshContainer) sshContainer.style.display = 'block';
    }
}

async function checkAndAutoLoginAwsSso() {
    const profile = document.getElementById('cfg-s3-profile')?.value?.trim() || 'iFlightCrew_Dev';
    addLog('info', `🔐 Verifying AWS SSO login status for profile '${profile}'...`);
    try {
        const response = await fetch('/api/check-aws-sso', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile: profile })
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        const contentType = response.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
            throw new Error('Non-JSON response from server');
        }
        const data = await response.json();
        if (data.active || data.authenticated) {
            addLog('success', `✅ AWS SSO Session is ACTIVE for profile '${profile}'! Ready to download.`);
        } else if (data.initiated_login) {
            addLog('warning', `🔑 AWS SSO session expired. Launched browser login for profile '${profile}'.`);
        } else if (data.error) {
            addLog('warning', `⚠️ AWS SSO check: ${data.error}`);
            triggerAwsSsoLogin();
        }
    } catch (e) {
        addLog('error', '❌ Could not verify AWS SSO status: ' + e.message);
        triggerAwsSsoLogin();
    }
}

async function triggerAwsSsoLogin() {
    const profile = document.getElementById('cfg-s3-profile')?.value?.trim() || 'iFlightCrew_Dev';
    addLog('info', `🔑 Launching AWS SSO Login for profile '${profile}'...`);
    try {
        const response = await fetch('/api/aws-sso-login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile: profile })
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        const contentType = response.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
            throw new Error('Non-JSON response from server');
        }
        const data = await response.json();
        if (data.error) {
            addLog('error', '❌ AWS SSO Login failed: ' + data.error);
        } else {
            addLog('success', '✅ ' + (data.message || 'AWS SSO login initialized.'));
        }
    } catch (e) {
        addLog('error', '❌ Network error during AWS SSO Login: ' + e.message);
    }
}

async function testS3Access() {
    const version = document.getElementById('version-input')?.value?.trim() || '3.96.34.267';
    addLog('info', `⚡ Testing AWS S3 access for version ${version}...`);
    try {
        const response = await fetch('/api/fetch-wars', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ version: version, download_source: 's3' })
        });
        const data = await response.json();
        if (data.error) {
            addLog('error', '❌ S3 Access Test Failed: ' + data.error);
            alert('S3 Access Test Failed: ' + data.error);
        } else {
            addLog('success', `✅ S3 Connection Successful! Discovered ${data.war_files.length} WAR files in bucket.`);
            alert(`S3 Access Verified! Found ${data.war_files.length} WAR files for version ${version}.`);
        }
    } catch (e) {
        addLog('error', '❌ S3 Test Error: ' + e.message);
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
        
        // Update header version badge
        const headerVersion = document.getElementById('header-version-text');
        if (headerVersion) {
            headerVersion.textContent = data.version;
        }
        
        document.getElementById('source-server').value = data.source_server;
        
        // Populate S3 details
        if (data.download_source) {
            setDownloadSource(data.download_source);
        }
        if (data.s3_bucket) {
            const bucketDisp = document.getElementById('s3-bucket-display');
            if (bucketDisp) bucketDisp.textContent = data.s3_bucket;
            const bucketCfg = document.getElementById('cfg-s3-bucket');
            if (bucketCfg) bucketCfg.value = data.s3_bucket;
        }
        if (data.s3_profile) {
            const profDisp = document.getElementById('s3-profile-display');
            if (profDisp) profDisp.textContent = data.s3_profile;
            const profCfg = document.getElementById('cfg-s3-profile');
            if (profCfg) profCfg.value = data.s3_profile;
        }
        if (data.s3_region) {
            const regCfg = document.getElementById('cfg-s3-region');
            if (regCfg) regCfg.value = data.s3_region;
        }
        if (data.s3_prefix_template) {
            const prefCfg = document.getElementById('cfg-s3-prefix-template');
            if (prefCfg) prefCfg.value = data.s3_prefix_template;
        }
        
        // Handle target server - show primary route with indicator for multiple routes
        const targetServerInput = document.getElementById('target-server');
        const targetUsernameInput = document.getElementById('target-username');
        
        targetServerInput.value = data.target_server;
        targetUsernameInput.value = data.target_username || '';
        
        // Add indicator for multiple routes
        if (data.total_routes && data.total_routes > 1) {
            targetServerInput.placeholder = `Primary route (${data.total_routes} routes configured)`;
            targetServerInput.title = `Using ${data.total_routes} PAM routes for load balancing. See Configuration tab for details.`;
        }
        
        document.getElementById('local-path').value = data.local_path;
        
        // Set parallel download checkbox state & threads count
        const parallelDownloadEl = document.getElementById('parallel-download');
        const parallelThreadsEl = document.getElementById('parallel-threads');
        if (parallelDownloadEl && data.parallel_downloads !== undefined) {
            parallelDownloadEl.checked = !!data.parallel_downloads;
            toggleParallelThreads(parallelDownloadEl.checked);
        }
        if (parallelThreadsEl && data.max_threads) {
            parallelThreadsEl.value = String(data.max_threads);
        }
        
        // Load WAR files list
        loadWarFilesList(data.war_files);
        
        // Add version change listener to highlight when modified
        versionInput.addEventListener('input', function() {
            // Sync with header badge
            if (headerVersion) {
                headerVersion.textContent = this.value || 'v?.?.?';
            }
            
            if (this.value !== data.version) {
                this.style.borderColor = '#fbbf24';
                this.style.boxShadow = '0 0 0 3px rgba(251, 191, 36, 0.2)';
            } else {
                this.style.borderColor = '';
                this.style.boxShadow = '';
            }
        });
        
        // Add change listeners for target fields to highlight when modified
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
    
    if (!warFiles || warFiles.length === 0) {
        container.innerHTML = '<div class="empty-state" style="padding: 10px; font-size: 11px;">No files found</div>';
        return;
    }
    
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
    
    // Reset filter to All when list is reloaded
    filterWarList('all');
}

// Filter the WAR files list
function filterWarList(category) {
    const items = document.querySelectorAll('.war-item');
    const pills = document.querySelectorAll('.filter-pill');
    
    // Update active pill
    pills.forEach(pill => {
        if (pill.getAttribute('data-filter') === category) {
            pill.classList.add('active');
        } else {
            pill.classList.remove('active');
        }
    });
    
    // Filter items
    items.forEach(item => {
        const value = item.querySelector('input').value.toLowerCase();
        
        if (category === 'all') {
            item.style.display = 'flex';
        } else if (category === 'crew') {
            if (value.includes('crew')) {
                item.style.display = 'flex';
            } else {
                item.style.display = 'none';
            }
        } else if (category === 'ops') {
            if (value.includes('ops') || value.includes('occ')) {
                item.style.display = 'flex';
            } else {
                item.style.display = 'none';
            }
        }
    });
}

// Fetch WAR files dynamically from source server
async function fetchWarsFromSource() {
    const version = document.getElementById('version-input').value.trim();
    if (!version) {
        alert('Please enter a version number first.');
        return;
    }
    
    const fetchBtn = document.getElementById('fetch-wars-btn');
    const fetchIcon = fetchBtn.querySelector('i');
    
    // UI Loading state
    fetchBtn.disabled = true;
    fetchIcon.classList.add('bi-spin'); // Custom class for rotation
    addLog('info', `📡 Fetching available WAR files for version ${version}...`);
    
    try {
        const response = await fetch('/api/fetch-wars', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ version: version, download_source: currentDownloadSource })
        });
        
        const data = await response.json();
        
        if (data.error) {
            addLog('error', '❌ Fetch failed: ' + data.error);
            if (!data.files || data.files.length === 0) {
                alert('No WAR files found for this version on the source server.');
            }
        } else {
            addLog('success', `✅ Discovered ${data.war_files.length} WAR files on source server`);
            loadWarFilesList(data.war_files);
            
            // Highlight the refresh button
            fetchBtn.style.borderColor = 'var(--accent-green)';
            fetchBtn.style.color = 'var(--accent-green)';
            setTimeout(() => {
                fetchBtn.style.borderColor = '';
                fetchBtn.style.color = '';
            }, 2000);
        }
    } catch (error) {
        console.error('Error fetching WAR files:', error);
        addLog('error', '❌ Network error during fetch: ' + error.message);
    } finally {
        fetchBtn.disabled = false;
        fetchIcon.classList.remove('bi-spin');
    }
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
    
    const stepNames = { 
        1: 'Download from Source', 
        2: 'Upload and Extract to Utilities',
        3: 'Deploy to Final Folders'
    };
    if (!confirm(`Run Step ${stepNumber}: ${stepNames[stepNumber]}\n\n${selectedWars.length} WAR file(s) for version ${version}?`)) {
        return;
    }
    
    await executeDeployment(selectedWars, [stepNumber], version);
}

// Run multiple steps together (Quick shortcuts)
async function runSteps(stepNumbers) {
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
    
    const stepNames = { 
        1: 'Download from Source', 
        2: 'Upload and Extract to Utilities',
        3: 'Deploy to Final Folders'
    };
    
    const stepList = stepNumbers.map(n => `  • Step ${n}: ${stepNames[n]}`).join('\n');
    if (!confirm(`Run Steps ${stepNumbers.join('+')}:\n\n${stepList}\n\n${selectedWars.length} WAR file(s) for version ${version}?`)) {
        return;
    }
    
    await executeDeployment(selectedWars, stepNumbers, version);
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
    
    if (!confirm(`Run FULL deployment of ${selectedWars.length} WAR file(s) for version ${version}?\n\nThis will run all 3 steps:\n- Step 1: Download from Source\n- Step 2: Upload & Extract to Utilities\n- Step 3: Deploy to Final Folders`)) {
        return;
    }
    
    await executeDeployment(selectedWars, [1, 2, 3], version);
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
    const parallelDownloadEl = document.getElementById('parallel-download');
    const parallelDownload = parallelDownloadEl ? parallelDownloadEl.checked : false;
    const parallelThreadsEl = document.getElementById('parallel-threads');
    const maxThreads = parallelThreadsEl ? (parseInt(parallelThreadsEl.value, 10) || 4) : 4;
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
    
    // Determine step description
    let stepDesc;
    if (steps.length === 1) {
        stepDesc = `Step ${steps[0]} only`;
    } else if (steps.length === 3) {
        stepDesc = 'All steps (1+2+3)';
    } else {
        stepDesc = `Steps ${steps.join('+')}`;
    }
    addLog('info', `📋 Steps: ${stepDesc}`);
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
                bypass_network: bypassNetwork,
                parallel_downloads: parallelDownload,
                max_threads: maxThreads,
                download_source: currentDownloadSource
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
    
    const cancelBtn = document.getElementById('cancel-btn');
    if (cancelBtn) {
        cancelBtn.disabled = true;
        const textSpan = cancelBtn.querySelector('span');
        if (textSpan) textSpan.textContent = 'Cancelling...';
    }
    
    try {
        await fetch('/api/cancel', { method: 'POST' });
        addLog('warning', 'Cancellation requested...');
    } catch (error) {
        addLog('error', 'Failed to cancel: ' + error.message);
        if (cancelBtn) {
            cancelBtn.disabled = false;
            const textSpan = cancelBtn.querySelector('span');
            if (textSpan) textSpan.textContent = 'Cancel';
        }
    }
}

async function retryFailed() {
    if (isDeploying) {
        alert('Deployment already in progress!');
        return;
    }

    const version = document.getElementById('version-input').value.trim();
    const targetServer = document.getElementById('target-server').value.trim();
    const targetUsername = document.getElementById('target-username').value.trim();

    if (!version) {
        alert('Please enter a version number.');
        return;
    }

    if (!confirm('Retry failed WAR uploads using alternate routes?')) {
        return;
    }

    const retryBtn = document.getElementById('retry-btn');
    if (retryBtn) retryBtn.style.display = 'none';

    clearLogs();
    addLog('info', '↻ Retrying failed uploads on alternate routes...');

    isDeploying = true;
    setButtonsDisabled(true);

    try {
        const response = await fetch('/api/retry-failed', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ version, target_server: targetServer, target_username: targetUsername })
        });

        const result = await response.json();

        if (!response.ok) {
            addLog('error', result.error || 'Retry failed to start');
            isDeploying = false;
            setButtonsDisabled(false);
            return;
        }

        addLog('success', `Retrying: ${result.retrying.join(', ')}`);
        // SSE stream already connected — completion will arrive via existing eventSource

    } catch (error) {
        addLog('error', 'Retry request failed: ' + error.message);
        isDeploying = false;
        setButtonsDisabled(false);
    }
}

// Enable/disable all deployment buttons and smartly toggle Cancel button
function setButtonsDisabled(disabled) {
    const deployBtn = document.getElementById('deploy-btn');
    const step1Btn = document.getElementById('step1-btn');
    const step2Btn = document.getElementById('step2-btn');
    const step3Btn = document.getElementById('step3-btn');
    const cancelBtn = document.getElementById('cancel-btn');
    
    if (deployBtn) deployBtn.disabled = disabled;
    if (step1Btn) step1Btn.disabled = disabled;
    if (step2Btn) step2Btn.disabled = disabled;
    if (step3Btn) step3Btn.disabled = disabled;
    
    // Smartly show Cancel button ONLY during active action/deployment
    if (cancelBtn) {
        cancelBtn.disabled = false;
        const textSpan = cancelBtn.querySelector('span');
        if (textSpan) textSpan.textContent = 'Cancel';
        cancelBtn.style.display = disabled ? 'inline-flex' : 'none';
    }
}

// Handle deployment completion
function handleDeploymentComplete(data) {
    isDeploying = false;
    setButtonsDisabled(false);
    
    const progressSection = document.querySelector('.progress-section');
    const deployBtn = document.getElementById('deploy-btn');
    const retryBtn = document.getElementById('retry-btn');
    
    deployBtn.classList.remove('deploying');
    
    if (data.cancelled) {
        updateStatus('Cancelled', 'bg-warning');
        if (progressSection) progressSection.classList.remove('active');
    } else {
        // Check for failed wars and show retry button if any
        const failedWars = data.failed_wars || [];
        if (failedWars.length > 0 && retryBtn) {
            retryBtn.style.display = 'flex';
            const names = failedWars.map(w => w.replace('iflight-','').replace('-webapp','').toUpperCase()).join(', ');
            retryBtn.querySelector('span').textContent = `Retry Failed (${failedWars.length})`;
            retryBtn.title = `Retry on alternate route: ${names}`;
        } else if (retryBtn) {
            retryBtn.style.display = 'none';
        }

        if (failedWars.length > 0) {
            updateStatus(`Done (${failedWars.length} failed)`, 'bg-warning');
        } else {
            updateStatus('Completed', 'bg-success');
            createConfetti();
        }
        if (progressSection) {
            progressSection.classList.remove('active');
            if (failedWars.length === 0) progressSection.classList.add('progress-complete');
        }
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

// Update file sizes display with modern card layout
function updateFileSizes(sizes) {
    fileSizes = sizes;
    const container = document.getElementById('file-sizes-body');
    
    const cards = Object.entries(sizes).map(([prefix, info]) => {
        const statusBadge = getStatusBadge(info.status);
        const sourceSize = info.source_size > 0 ? formatSize(info.source_size) : '-';
        const targetSize = info.target_size > 0 ? formatSize(info.target_size) : '-';
        
        // Show transfer progress if actively downloading/uploading
        let progressBar = '';
        const isDownloading = (info.status === 'downloading' || info.status === 'processing' || info.status === 'uploading' || info.status === 'extracting');
        if (isDownloading) {
            const totalBytes = info.total_size || info.source_size || 0;
            const transferredBytes = info.transferred || 0;
            const calcPercent = totalBytes > 0 ? (transferredBytes / totalBytes * 100) : (info.transfer_progress || 0);
            const percent = Math.min(100, Math.max(0, calcPercent)).toFixed(1);
            const transferred = formatSize(transferredBytes);
            const total = totalBytes > 0 ? formatSize(totalBytes) : 'Calculating...';
            progressBar = `
                <div class="file-card-progress">
                    <div class="transfer-progress">
                        <div class="progress-wrapper">
                            <div class="progress-bar-container">
                                <div class="progress-bar-fill" style="width: ${percent}%"></div>
                            </div>
                            <div class="progress-text">${percent}%</div>
                        </div>
                        <div class="transfer-size">${transferred} / ${total}</div>
                    </div>
                </div>
            `;
        }
        
        return `
            <div class="file-card">
                <div class="file-card-header">
                    <div class="file-card-name">
                        <i class="bi bi-file-earmark-code-fill"></i>
                        ${info.name || prefix}
                    </div>
                    <div class="file-card-status">
                        ${statusBadge}
                    </div>
                </div>
                <div class="file-card-body">
                    <div class="file-size-item">
                        <div class="file-size-label"><i class="bi bi-download"></i> Source</div>
                        <div class="file-size-value">${sourceSize}</div>
                    </div>
                    <div class="file-size-item">
                        <div class="file-size-label"><i class="bi bi-upload"></i> Target</div>
                        <div class="file-size-value">${targetSize}</div>
                    </div>
                </div>
                ${progressBar}
            </div>
        `;
    }).join('');
    
    container.innerHTML = cards || '<div class="empty-state"><i class="bi bi-inbox"></i><span>Run deployment to see file sizes</span></div>';
}

// Get status badge HTML with icons
function getStatusBadge(status) {
    const badges = {
        'pending': '<span class="status-badge status-pending"><i class="bi bi-clock"></i> Pending</span>',
        'processing': '<span class="status-badge status-downloading"><i class="bi bi-gear-fill"></i> Processing</span>',
        'downloading': '<span class="status-badge status-downloading"><i class="bi bi-cloud-download-fill"></i> Downloading</span>',
        'downloaded': '<span class="status-badge status-downloaded"><i class="bi bi-check-circle-fill"></i> Downloaded</span>',
        'uploading': '<span class="status-badge status-uploading"><i class="bi bi-cloud-upload-fill"></i> Uploading</span>',
        'extracting': '<span class="status-badge status-extracting"><i class="bi bi-file-zip-fill"></i> Extracting</span>',
        'extracted': '<span class="status-badge status-downloaded"><i class="bi bi-box-fill"></i> Extracted</span>',
        'deploying': '<span class="status-badge status-uploading"><i class="bi bi-rocket-fill"></i> Deploying</span>',
        'deployed': '<span class="status-badge status-deployed"><i class="bi bi-check-circle-fill"></i> Deployed</span>',
        'error': '<span class="status-badge status-error"><i class="bi bi-x-circle-fill"></i> Error</span>',
        'warning': '<span class="status-badge status-pending"><i class="bi bi-exclamation-triangle-fill"></i> Warning</span>'
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

// Update progress bar with enhanced animations & telemetry state
function updateProgress(progress, currentFile, completed, total) {
    const progressBar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');
    const currentFileSpan = document.getElementById('current-file');
    const completedSpan = document.getElementById('completed-count');
    const totalSpan = document.getElementById('total-count');
    const progressSection = document.querySelector('.progress-hero-card') || document.querySelector('.progress-section');
    
    const roundedProgress = Math.round(progress);
    
    if (progressBar) progressBar.style.width = roundedProgress + '%';
    if (progressText) progressText.textContent = roundedProgress + '%';
    
    // Telemetry Badge & Pipeline Stepper Node Updates
    const telemetryBadge = document.getElementById('telemetry-status-badge');
    const telemetryText = document.getElementById('telemetry-status-text');
    const s1 = document.getElementById('step-stage-1');
    const s2 = document.getElementById('step-stage-2');
    const s3 = document.getElementById('step-stage-3');

    if (telemetryBadge) {
        if (roundedProgress > 0 && roundedProgress < 100) {
            telemetryBadge.className = 'telemetry-badge running';
            if (telemetryText) telemetryText.textContent = 'Executing';
        } else if (roundedProgress >= 100) {
            telemetryBadge.className = 'telemetry-badge complete';
            if (telemetryText) telemetryText.textContent = 'Completed';
        } else {
            telemetryBadge.className = 'telemetry-badge idle';
            if (telemetryText) telemetryText.textContent = 'Standby';
        }
    }

    // Stepper updates
    if (s1 && s2 && s3) {
        if (roundedProgress === 0) {
            s1.className = 'step-item'; s2.className = 'step-item'; s3.className = 'step-item';
        } else if (roundedProgress > 0 && roundedProgress <= 33) {
            s1.className = 'step-item active'; s2.className = 'step-item'; s3.className = 'step-item';
        } else if (roundedProgress > 33 && roundedProgress <= 75) {
            s1.className = 'step-item complete'; s2.className = 'step-item active'; s3.className = 'step-item';
        } else if (roundedProgress > 75 && roundedProgress < 100) {
            s1.className = 'step-item complete'; s2.className = 'step-item complete'; s3.className = 'step-item active';
        } else if (roundedProgress >= 100) {
            s1.className = 'step-item complete'; s2.className = 'step-item complete'; s3.className = 'step-item complete';
        }
    }
    
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
    
    if (currentFile && currentFileSpan) {
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
        if (completedSpan && parseInt(completedSpan.textContent) !== completed) {
            completedSpan.classList.add('counter');
        }
        if (completedSpan) completedSpan.textContent = completed;
        if (totalSpan) totalSpan.textContent = total;
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

// ================================================
// TAB MANAGEMENT & CONFIGURATION FUNCTIONS
// ================================================

// Switch between tabs
function switchTab(tabName) {
    console.log('Switching to tab:', tabName);
    
    // Update tab buttons
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active');
        if (btn.dataset.tab === tabName) {
            btn.classList.add('active');
        }
    });
    
    // Update tab content
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.remove('active');
    });
    
    const targetTab = document.getElementById(tabName + '-tab');
    if (targetTab) {
        targetTab.classList.add('active');
        console.log('Tab activated:', tabName);
        
        // Debug visibility
        const computedStyle = window.getComputedStyle(targetTab);
        console.log('Tab display:', computedStyle.display);
        console.log('Tab opacity:', computedStyle.opacity);
        console.log('Tab visibility:', computedStyle.visibility);
    }
    
    // Toggle fullscreen config mode
    if (tabName === 'config') {
        console.log('Enabling fullscreen mode');
        document.body.classList.add('config-fullscreen-mode');
        
        // Small delay to ensure CSS is applied
        setTimeout(() => {
            const scrollContent = document.querySelector('#config-tab .tab-content-scroll');
            if (scrollContent) {
                console.log('Scroll content found, children:', scrollContent.children.length);
                console.log('Scroll content display:', window.getComputedStyle(scrollContent).display);
                console.log('Scroll content height:', scrollContent.offsetHeight);
                console.log('Scroll content scroll height:', scrollContent.scrollHeight);
                
                // Check if config hero is visible
                const configHero = document.querySelector('.config-hero');
                if (configHero) {
                    const heroStyle = window.getComputedStyle(configHero);
                    console.log('Hero display:', heroStyle.display);
                    console.log('Hero opacity:', heroStyle.opacity);
                    console.log('Hero visibility:', heroStyle.visibility);
                    console.log('Hero height:', configHero.offsetHeight);
                }
            } else {
                console.error('Scroll content not found!');
            }
            
            loadAdvancedConfiguration();
        }, 100);
    } else {
        console.log('Disabling fullscreen mode');
        document.body.classList.remove('config-fullscreen-mode');
    }
}

// Close config fullscreen mode
function closeConfigFullscreen() {
    switchTab('deploy');
}

// Load advanced configuration into the config tab
async function loadAdvancedConfiguration() {
    try {
        const response = await fetch('/api/config/advanced');
        const data = await response.json();
        
        if (data.error) {
            showConfigNotification('Failed to load configuration', 'error');
            return;
        }
        
        // Populate source server fields
        document.getElementById('cfg-source-host').value = data.source_server?.host || '';
        document.getElementById('cfg-source-username').value = data.source_server?.username || '';
        document.getElementById('cfg-source-port').value = data.source_server?.port || 22;
        document.getElementById('cfg-source-switch-user').value = data.source_server?.switch_user || '';
        
        // Populate S3 fields
        const s3Cfg = data.s3_config || {};
        const bucketEl = document.getElementById('cfg-s3-bucket');
        if (bucketEl) bucketEl.value = s3Cfg.bucket || 'iflightdevrdits3';
        const profileEl = document.getElementById('cfg-s3-profile');
        if (profileEl) profileEl.value = s3Cfg.profile || 'iFlightCrew_Dev';
        const regionEl = document.getElementById('cfg-s3-region');
        if (regionEl) regionEl.value = s3Cfg.region || 'ap-south-1';
        const prefixEl = document.getElementById('cfg-s3-prefix-template');
        if (prefixEl) prefixEl.value = s3Cfg.prefix_template || 'iFlight_Release/{version}/Wars/';
        
        // Populate target server fields - handle both routes array and legacy single server
        const targetServer = data.target_server || {};
        document.getElementById('cfg-target-port').value = targetServer.port || 22;
        
        // Load target routes
        loadTargetRoutes(targetServer.routes || []);
        
        // Populate paths
        document.getElementById('cfg-local-path').value = data.local?.download_path || '';
        document.getElementById('cfg-source-base').value = data.paths?.source_base || '';
        document.getElementById('cfg-target-utilities').value = data.paths?.target_utilities || '';
        document.getElementById('cfg-target-deploy-base').value = data.paths?.target_deploy_base || '';
        
        // Load WAR mappings
        loadWarMappings(data.war_mappings || {});
        
    } catch (error) {
        console.error('Failed to load configuration:', error);
        showConfigNotification('Failed to load configuration: ' + error.message, 'error');
    }
}

// Load target routes into the list
function loadTargetRoutes(routes) {
    const container = document.getElementById('target-routes-list');
    container.innerHTML = '';
    
    if (!routes || routes.length === 0) {
        // Add one empty route by default
        addTargetRoute('', '');
    } else {
        routes.forEach(route => {
            addTargetRoute(route.host || '', route.username || '');
        });
    }
}

// Add a target route item to the list
function addTargetRoute(host = '', username = '') {
    const container = document.getElementById('target-routes-list');
    const item = document.createElement('div');
    item.className = 'route-item';
    item.innerHTML = `
        <input type="text" class="config-input" placeholder="PAM_NV.ibsplc.aero" value="${host}" data-field="host">
        <input type="text" class="config-input" placeholder="user@domain%context%ip" value="${username}" data-field="username">
        <button class="btn-remove-route" onclick="removeTargetRoute(this)" title="Remove Route">
            <i class="bi bi-trash-fill"></i>
        </button>
    `;
    container.appendChild(item);
}

// Remove target route
function removeTargetRoute(btn) {
    const container = document.getElementById('target-routes-list');
    const item = btn.closest('.route-item');
    
    // Don't allow removing the last route
    if (container.children.length <= 1) {
        showConfigNotification('At least one route is required', 'error');
        return;
    }
    
    item.remove();
}

// Load WAR mappings into the list
function loadWarMappings(mappings) {
    const container = document.getElementById('war-mappings-list');
    container.innerHTML = '';
    
    Object.entries(mappings).forEach(([warPrefix, folder]) => {
        addWarMappingItem(warPrefix, folder);
    });
}

// Add a WAR mapping item to the list
function addWarMappingItem(warPrefix = '', folder = '') {
    const container = document.getElementById('war-mappings-list');
    const item = document.createElement('div');
    item.className = 'mapping-item';
    item.innerHTML = `
        <input type="text" class="mapping-input" placeholder="iflight-crew-...-webapp" value="${warPrefix}" data-field="prefix">
        <input type="text" class="mapping-input" placeholder="CREW_XXX" value="${folder}" data-field="folder">
        <button class="btn-remove-mapping" onclick="removeWarMapping(this)" title="Remove">
            <i class="bi bi-trash-fill"></i>
        </button>
    `;
    container.appendChild(item);
}

// Add new WAR mapping
function addWarMapping() {
    addWarMappingItem('', '');
    // Scroll to bottom
    const container = document.getElementById('war-mappings-list');
    container.scrollTop = container.scrollHeight;
}

// Remove WAR mapping
function removeWarMapping(btn) {
    btn.closest('.mapping-item').remove();
}

// Save configuration
async function saveConfiguration() {
    try {
        // Gather all configuration data
        const configData = {
            version: document.getElementById('version-input').value,
            download_source: currentDownloadSource,
            s3_config: {
                bucket: document.getElementById('cfg-s3-bucket')?.value?.trim() || 'iflightdevrdits3',
                profile: document.getElementById('cfg-s3-profile')?.value?.trim() || 'iFlightCrew_Dev',
                region: document.getElementById('cfg-s3-region')?.value?.trim() || 'ap-south-1',
                prefix_template: document.getElementById('cfg-s3-prefix-template')?.value?.trim() || 'iFlight_Release/{version}/Wars/'
            },
            source_server: {
                host: document.getElementById('cfg-source-host').value,
                username: document.getElementById('cfg-source-username').value,
                port: parseInt(document.getElementById('cfg-source-port').value) || 22,
                switch_user: document.getElementById('cfg-source-switch-user').value
            },
            target_server: {
                routes: [],
                port: parseInt(document.getElementById('cfg-target-port').value) || 22
            },
            local: {
                download_path: document.getElementById('cfg-local-path').value
            },
            paths: {
                source_base: document.getElementById('cfg-source-base').value,
                target_utilities: document.getElementById('cfg-target-utilities').value,
                target_deploy_base: document.getElementById('cfg-target-deploy-base').value
            },
            war_mappings: {}
        };
        
        // Gather target routes
        document.querySelectorAll('.route-item').forEach(item => {
            const host = item.querySelector('[data-field="host"]').value.trim();
            const username = item.querySelector('[data-field="username"]').value.trim();
            if (host && username) {
                configData.target_server.routes.push({ host, username });
            }
        });
        
        // Validate at least one route
        if (configData.target_server.routes.length === 0) {
            showConfigNotification('At least one target route is required', 'error');
            return;
        }
        
        // Gather WAR mappings
        document.querySelectorAll('.mapping-item').forEach(item => {
            const prefix = item.querySelector('[data-field="prefix"]').value.trim();
            const folder = item.querySelector('[data-field="folder"]').value.trim();
            if (prefix && folder) {
                configData.war_mappings[prefix] = folder;
            }
        });
        
        // Validate required fields
        if (!configData.version) {
            showConfigNotification('Version is required', 'error');
            return;
        }
        
        // Save to server
        const response = await fetch('/api/config/advanced', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(configData)
        });
        
        const result = await response.json();
        
        if (result.success) {
            showConfigNotification('✓ Configuration saved successfully!', 'success');
            // Reload the main configuration
            await loadConfiguration();
        } else {
            showConfigNotification('Failed to save: ' + (result.error || 'Unknown error'), 'error');
        }
        
    } catch (error) {
        console.error('Failed to save configuration:', error);
        showConfigNotification('Failed to save configuration: ' + error.message, 'error');
    }
}

// Reset configuration to defaults
async function resetConfiguration() {
    if (!confirm('Are you sure you want to reset all configuration to defaults? This cannot be undone.')) {
        return;
    }
    
    try {
        const response = await fetch('/api/config/reset', {
            method: 'POST'
        });
        
        const result = await response.json();
        
        if (result.success) {
            showConfigNotification('✓ Configuration reset to defaults', 'success');
            await loadAdvancedConfiguration();
            await loadConfiguration();
        } else {
            showConfigNotification('Failed to reset: ' + (result.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Failed to reset configuration:', error);
        showConfigNotification('Failed to reset configuration: ' + error.message, 'error');
    }
}

// Show configuration notification
function showConfigNotification(message, type = 'success') {
    const existing = document.querySelector('.config-notification');
    if (existing) {
        existing.remove();
    }
    
    const notification = document.createElement('div');
    notification.className = `config-notification ${type}`;
    notification.innerHTML = `
        <i class="bi bi-${type === 'success' ? 'check-circle-fill' : 'exclamation-triangle-fill'}"></i>
        <span>${message}</span>
    `;
    
    const configTab = document.querySelector('#config-tab .tab-content-scroll');
    if (configTab) {
        configTab.insertBefore(notification, configTab.firstChild);
        
        // Auto-remove after 5 seconds
        setTimeout(() => {
            notification.style.opacity = '0';
            notification.style.transform = 'translateY(-10px)';
            setTimeout(() => notification.remove(), 300);
        }, 5000);
    }
}

// ================================================
// PASSWORD MANAGEMENT FUNCTIONS
// ================================================

// Toggle password visibility
function togglePassword(inputId) {
    const input = document.getElementById(inputId);
    const btn = input.parentElement.querySelector('.btn-toggle-password');
    const icon = btn.querySelector('i');
    
    if (input.type === 'password') {
        input.type = 'text';
        icon.classList.remove('bi-eye-fill');
        icon.classList.add('bi-eye-slash-fill');
        btn.classList.add('active');
    } else {
        input.type = 'password';
        icon.classList.remove('bi-eye-slash-fill');
        icon.classList.add('bi-eye-fill');
        btn.classList.remove('active');
    }
}

// Clear password fields
function clearPasswordFields() {
    document.getElementById('cfg-source-password').value = '';
    document.getElementById('cfg-target-password').value = '';
    
    // Reset to password type
    const inputs = ['cfg-source-password', 'cfg-target-password'];
    inputs.forEach(id => {
        const input = document.getElementById(id);
        if (input.type === 'text') {
            togglePassword(id);
        }
    });
}

// Save passwords to .env file
async function savePasswords() {
    const sourcePassword = document.getElementById('cfg-source-password').value;
    const targetPassword = document.getElementById('cfg-target-password').value;
    
    if (!sourcePassword && !targetPassword) {
        showConfigNotification('Please enter at least one password to update', 'error');
        return;
    }
    
    if (!confirm('Are you sure you want to update the passwords? This will modify your .env file.')) {
        return;
    }
    
    try {
        const data = {};
        if (sourcePassword) {
            data.source_password = sourcePassword;
        }
        if (targetPassword) {
            data.target_password = targetPassword;
        }
        
        const response = await fetch('/api/config/passwords', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });
        
        const result = await response.json();
        
        if (result.success) {
            showConfigNotification('✓ Passwords updated securely in .env file', 'success');
            clearPasswordFields();
        } else {
            showConfigNotification('Failed to update passwords: ' + (result.error || 'Unknown error'), 'error');
        }
        
    } catch (error) {
        console.error('Failed to save passwords:', error);
        showConfigNotification('Failed to save passwords: ' + error.message, 'error');
    }
}

// Toggle visibility of parallel thread count dropdown
function toggleParallelThreads(checked) {
    const container = document.getElementById('parallel-threads-container');
    if (container) {
        container.style.display = checked ? 'inline-flex' : 'none';
    }
}
