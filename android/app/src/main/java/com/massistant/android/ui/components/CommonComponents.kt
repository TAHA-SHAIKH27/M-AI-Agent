package com.massistant.android.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.Shield
import androidx.compose.material.icons.filled.Sync
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.massistant.android.ui.theme.warning

@Composable
fun MessageBubble(
    message: String,
    isUser: Boolean,
    modifier: Modifier = Modifier,
    onLongClick: (() -> Unit)? = null
) {
    val colors = MaterialTheme.colorScheme
    val shape = RoundedCornerShape(16.dp)
    
    val clickableModifier = modifier
        .fillMaxWidth()
        .padding(horizontal = 16.dp, vertical = 4.dp)
        .let { mod ->
            if (onLongClick != null) {
                mod.combinedClickable(
                    onLongClick = onLongClick,
                    onClick = {}
                )
            } else {
                mod
            }
        }
    
    Surface(
        modifier = clickableModifier,
        color = if (isUser) colors.primary else colors.surfaceVariant,
        shape = shape,
        tonalElevation = 2.dp
    ) {
        Text(
            text = message,
            color = if (isUser) colors.onPrimary else colors.onSurface,
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.padding(16.dp)
        )
    }
}

@Composable
fun MessageList(
    messages: List<com.massistant.android.data.model.ChatMessage>,
    modifier: Modifier = Modifier
) {
    androidx.compose.foundation.lazy.LazyColumn(
        modifier = modifier.fillMaxSize(),
        reverseLayout = true,
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        items(messages.reversed()) { message ->
            val isUser = message.role == "user"
            MessageBubble(
                message = message.content,
                isUser = isUser,
                modifier = Modifier
                    .fillMaxWidth()
                    .wrapContentWidth(Alignment.End)
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatInput(
    onSend: (String) -> Unit,
    modifier: Modifier = Modifier,
    isRecording: Boolean = false,
    onVoiceClick: () -> Unit,
    enabled: Boolean = true
) {
    var text by remember { mutableStateOf("") }
    val colors = MaterialTheme.colorScheme
    
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(16.dp)
            .background(colors.surface),
        verticalAlignment = Alignment.CenterVertically
    ) {
        // Voice button
        IconButton(
            onClick = onVoiceClick,
            enabled = enabled,
            modifier = Modifier.size(48.dp)
        ) {
            Icon(
                imageVector = if (isRecording) 
                    Icons.Default.Mic 
                else 
                    Icons.Default.Mic,
                contentDescription = if (isRecording) "Stop recording" else "Start voice input",
                tint = if (isRecording) colors.error else colors.onSurfaceVariant
            )
        }
        
        // Text input
        TextField(
            value = text,
            onValueChange = { text = it },
            modifier = Modifier
                .weight(1f)
                .height(48.dp)
                .padding(horizontal = 8.dp),
            singleLine = true,
            placeholder = { Text("Type a message…", color = colors.onSurfaceVariant.copy(alpha = 0.6f)) },
            colors = TextFieldDefaults.colors(
                focusedContainerColor = colors.surfaceVariant,
                unfocusedContainerColor = colors.surfaceVariant,
                focusedTextColor = colors.onSurface,
                unfocusedTextColor = colors.onSurface,
                focusedPlaceholderColor = colors.onSurfaceVariant.copy(alpha = 0.6f),
                unfocusedPlaceholderColor = colors.onSurfaceVariant.copy(alpha = 0.6f)
            ),
            shape = RoundedCornerShape(24.dp)
        )
        
        // Send button
        IconButton(
            onClick = { if (text.isNotBlank()) { onSend(text); text = "" } },
            enabled = enabled && text.isNotBlank(),
            modifier = Modifier.size(48.dp)
        ) {
            Icon(
                imageVector = Icons.Default.Send,
                contentDescription = "Send message",
                tint = colors.primary
            )
        }
    }
}

@Composable
fun ConnectionStatusIndicator(
    state: com.massistant.android.data.api.WebSocketClient.ConnectionState,
    modifier: Modifier = Modifier
) {
    val (icon, color, text) = when (state) {
        is com.massistant.android.data.api.WebSocketClient.ConnectionState.Connecting -> 
            Triple(Icons.Default.Sync, MaterialTheme.colorScheme.warning, "Connecting…")
        is com.massistant.android.data.api.WebSocketClient.ConnectionState.Connected -> 
            Triple(Icons.Default.CheckCircle, MaterialTheme.colorScheme.tertiary, "Connected")
        is com.massistant.android.data.api.WebSocketClient.ConnectionState.Disconnected -> 
            Triple(Icons.Default.WifiOff, MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f), "Disconnected")
        is com.massistant.android.data.api.WebSocketClient.ConnectionState.Error -> 
            Triple(Icons.Default.Error, MaterialTheme.colorScheme.error, "Error")
    }
    
    Row(
        modifier = modifier.padding(16.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Icon(icon, contentDescription = null, tint = color, modifier = Modifier.size(20.dp))
        Text(text, color = color, style = MaterialTheme.typography.labelMedium)
    }
}

@Composable
fun ToolCard(
    tool: com.massistant.android.data.model.ToolManifest,
    onClick: () -> Unit,
    modifier: Modifier = Modifier
) {
    val colors = MaterialTheme.colorScheme
    val riskColor = when (tool.risk_level) {
        "low" -> colors.tertiary
        "medium" -> colors.warning
        "high" -> colors.warning.copy(alpha = 0.8f)
        "critical" -> colors.error
        else -> colors.onSurfaceVariant
    }
    
    Card(
        modifier = modifier.fillMaxWidth(),
        onClick = onClick,
        elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(
            containerColor = colors.surfaceVariant
        )
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column {
                    Text(tool.display_name, style = MaterialTheme.typography.titleMedium, color = colors.onSurface)
                    Text(tool.description, style = MaterialTheme.typography.bodySmall, color = colors.onSurfaceVariant, maxLines = 2)
                }
                Box(
                    modifier = Modifier
                        .padding(8.dp)
                        .width(8.dp)
                        .height(8.dp)
                        .background(riskColor, RoundedCornerShape(50))
                )
            }
            
            if (tool.requires_approval) {
                Row(
                    modifier = Modifier.padding(top = 8.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Icon(
                        Shield,
                        contentDescription = null,
                        tint = colors.warning,
                        modifier = Modifier.size(16.dp)
                    )
                    Text("Requires approval", style = MaterialTheme.typography.labelSmall, color = colors.warning)
                }
            }
        }
    }
}

@Composable
fun ApprovalDialog(
    payload: com.massistant.android.data.model.ApprovalRequiredPayload,
    onApprove: () -> Unit,
    onReject: () -> Unit,
    modifier: Modifier = Modifier
) {
    val colors = MaterialTheme.colorScheme
    
    AlertDialog(
        onDismissRequest = onReject,
        title = { Text("Approval Required", style = MaterialTheme.typography.headlineSmall) },
        text = {
            Column(modifier = Modifier.padding(top = 8.dp)) {
                Text(payload.description, style = MaterialTheme.typography.bodyMedium)
                if (payload.tool_name != null) {
                    Text("Tool: ${payload.tool_name}", style = MaterialTheme.typography.labelMedium, color = colors.onSurfaceVariant)
                }
                Text("Risk Level: ${payload.risk_level}", style = MaterialTheme.typography.labelSmall, color = when (payload.risk_level) {
                    "critical" -> colors.error
                    "high" -> colors.warning
                    else -> colors.onSurfaceVariant
                })
            }
        },
        confirmButton = {
            Button(
                onClick = { onApprove() },
                colors = ButtonDefaults.buttonColors(containerColor = colors.tertiary)
            ) {
                Text("Approve")
            }
        },
        dismissButton = {
            Button(
                onClick = { onReject() },
                colors = ButtonDefaults.buttonColors(containerColor = colors.error)
            ) {
                Text("Reject")
            }
        }
    )
}

@Composable
fun LoadingOverlay(
    message: String = "Processing…",
    modifier: Modifier = Modifier
) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .background(Color.Black.copy(alpha = 0.5f)),
        contentAlignment = androidx.compose.ui.Alignment.Center
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
            Text(message, color = Color.White, style = MaterialTheme.typography.bodyLarge)
        }
    }
}