package com.massistant.android.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.ui.graphics.Color
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.massistant.android.data.api.WebSocketClient
import com.massistant.android.data.model.ChatMessage
import com.massistant.android.data.model.TaskProgressPayload
import com.massistant.android.data.model.TaskCompletedPayload
import com.massistant.android.data.model.ApprovalRequiredPayload
import com.massistant.android.data.repository.ChatRepository
import com.massistant.android.ui.components.ApprovalDialog
import com.massistant.android.ui.components.ChatInput
import com.massistant.android.ui.components.ConnectionStatusIndicator
import com.massistant.android.ui.components.LoadingOverlay
import com.massistant.android.ui.components.MessageList
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    repository: ChatRepository,
    onSettingsClick: () -> Unit
) {
    val messages by repository.messages.collectAsState()
    val connectionState by repository.connectionState.collectAsState()
    var showApprovalDialog by remember { mutableStateOf(false) }
    var approvalPayload by remember { mutableStateOf<ApprovalRequiredPayload?>(null) }
    var isProcessing by remember { mutableStateOf(false) }
    var isVoiceRecording by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    // Handle incoming WebSocket messages
    LaunchedEffect(repository) {
        repository.handleIncomingMessages(
            onMessage = { msg ->
                // Assistant message received via WebSocket
            },
            onTaskProgress = { progress ->
                // Update UI with task progress
            },
            onTaskCompleted = { completed ->
                // Task completed
            },
            onApprovalRequired = { payload ->
                approvalPayload = payload
                showApprovalDialog = true
            }
        )
    }

    val sendMessage = { text: String ->
        isProcessing = true
        scope.launch {
            try { repository.sendMessage(text) } finally { isProcessing = false }
        }
    }

    val startVoiceRecording = {
        isVoiceRecording = true
        // TODO: Implement voice recording
    }

    val stopVoiceRecording = {
        isVoiceRecording = false
        // TODO: Implement voice recording stop
    }

    val handleApprove = {
        approvalPayload?.let { payload ->
            repository.sendApproval(payload.task_id, payload.subtask_id, true)
        }
        showApprovalDialog = false
        approvalPayload = null
    }

    val handleReject = {
        approvalPayload?.let { payload ->
            repository.sendApproval(payload.task_id, payload.subtask_id, false)
        }
        showApprovalDialog = false
        approvalPayload = null
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("M Companion") },
                navigationIcon = { 
                    IconButton(onClick = onSettingsClick) {
                        Icon(Icons.Default.Menu, contentDescription = "Menu")
                    }
                },
                actions = {
                    ConnectionStatusIndicator(state = connectionState)
                    IconButton(onClick = onSettingsClick) {
                        Icon(Icons.Default.Settings, contentDescription = "Settings")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.surface)
            )
        },
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
        ) {
            Column(
                modifier = Modifier.fillMaxSize(),
                verticalArrangement = Arrangement.Top
            ) {
                // Messages
                MessageList(
                    messages = messages,
                    modifier = Modifier
                        .fillMaxSize()
                        .weight(1f)
                        .padding(bottom = 16.dp)
                )

                // Typing indicator
                isProcessing.let { processing ->
                    if (processing) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 8.dp),
                            horizontalArrangement = Arrangement.Start
                        ) {
                            MessageBubble(
                                message = "M is thinking…",
                                isUser = false,
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .wrapContentWidth(Alignment.Start)
                            )
                        }
                    }
                }

                // Input
                ChatInput(
                    onSend = sendMessage,
                    isRecording = isVoiceRecording,
                    onVoiceClick = {
                        if (isVoiceRecording) stopVoiceRecording() else startVoiceRecording()
                    },
                    enabled = connectionState is WebSocketClient.ConnectionState.Connected
                )
            }

            // Loading overlay
            if (isProcessing) {
                LoadingOverlay(message = "Processing…")
            }

            // Approval dialog
            if (showApprovalDialog) {
                approvalPayload?.let { payload ->
                    ApprovalDialog(
                        payload = payload,
                        onApprove = handleApprove,
                        onReject = handleReject
                    )
                }
            }
        }
    }
}

@Composable
fun DrawerContent(
    onSettingsClick: () -> Unit
) {
    val colors = MaterialTheme.colorScheme
    
    Column(
        modifier = Modifier
            .fillMaxSize()
            .width(280.dp)
            .background(colors.surface),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        androidx.compose.foundation.layout.Spacer(modifier = Modifier.padding(top = 48.dp))
        
        // App logo/name
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(
                Icons.Default.SmartToy,
                contentDescription = null,
                tint = colors.primary,
                modifier = Modifier.size(64.dp)
            )
            androidx.compose.foundation.layout.Spacer(modifier = Modifier.padding(top = 16.dp))
            Text("M Companion", style = MaterialTheme.typography.headlineMedium, color = colors.onSurface)
            Text("Version 1.0.0", style = MaterialTheme.typography.bodySmall, color = colors.onSurfaceVariant)
        }
        
        androidx.compose.foundation.layout.Spacer(modifier = Modifier.padding(top = 32.dp))
        
        Divider(modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp))
        
        androidx.compose.foundation.layout.Spacer(modifier = Modifier.padding(top = 16.dp))
        
        // Navigation items
        Column(modifier = Modifier.padding(horizontal = 16.dp)) {
            NavigationItem(
                icon = Icons.Default.Chat,
                text = "Chat",
                selected = true,
                onClick = {}
            )
            NavigationItem(
                icon = Icons.Default.History,
                text = "History",
                selected = false,
                onClick = {}
            )
            NavigationItem(
                icon = Icons.Default.TaskAlt,
                text = "Tasks",
                selected = false,
                onClick = {}
            )
            NavigationItem(
                icon = Icons.Default.Settings,
                text = "Settings",
                selected = false,
                onClick = onSettingsClick
            )
            NavigationItem(
                icon = Icons.Default.Info,
                text = "About",
                selected = false,
                onClick = {}
            )
        }
    }
}

@Composable
fun NavigationItem(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    text: String,
    selected: Boolean,
    onClick: () -> Unit
) {
    val colors = MaterialTheme.colorScheme
    
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 12.dp)
            .background(
                if (selected) colors.primaryContainer else Color.Transparent,
                shape = androidx.compose.foundation.shape.RoundedCornerShape(12.dp)
            )
            .clickable(onClick = onClick),
        horizontalArrangement = Arrangement.spacedBy(16.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Icon(icon, contentDescription = null, tint = if (selected) colors.primary else colors.onSurfaceVariant, modifier = Modifier.size(24.dp))
        Text(text, style = MaterialTheme.typography.bodyLarge, color = if (selected) colors.primary else colors.onSurface)
    }
}

@Composable
fun ChatScreenPreview() {
    // Preview would need a mock repository
}