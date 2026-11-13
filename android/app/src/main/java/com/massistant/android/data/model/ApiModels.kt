package com.massistant.android.data.model

import kotlinx.serialization.Serializable
import kotlinx.serialization.SerialName
import kotlinx.serialization.json.JsonElement

// Chat Models
@Serializable
data class ChatRequest(
    val message: String,
    val conversation_id: String? = null,
    val stream: Boolean = true,
    val metadata: Map<String, String> = emptyMap()
)

@Serializable
data class ChatResponse(
    val content: String,
    val conversation_id: String,
    val task_id: String? = null,
    val success: Boolean = true
)

@Serializable
data class ChatMessage(
    val role: String, // "user", "assistant", "system", "tool"
    val content: String,
    val timestamp: Long = System.currentTimeMillis()
)

// WebSocket Models
@Serializable
sealed class WSMessage {
    @Serializable
    @SerialName("chat")
    data class Chat(
        val payload: ChatPayload
    ) : WSMessage()

    @Serializable
    @SerialName("task_progress")
    data class TaskProgress(
        val payload: TaskProgressPayload
    ) : WSMessage()

    @Serializable
    @SerialName("task_completed")
    data class TaskCompleted(
        val payload: TaskCompletedPayload
    ) : WSMessage()

    @Serializable
    @SerialName("approval_required")
    data class ApprovalRequired(
        val payload: ApprovalRequiredPayload
    ) : WSMessage()

    @Serializable
    @SerialName("error")
    data class Error(
        val payload: ErrorPayload
    ) : WSMessage()

    @Serializable
    @SerialName("ping")
    object Ping : WSMessage()

    @Serializable
    @SerialName("pong")
    object Pong : WSMessage()

    @Serializable
    @SerialName("ack")
    data class Ack(
        val client_id: String,
        val connection_id: String
    ) : WSMessage()
}

@Serializable
data class ChatPayload(
    val message: String,
    val conversation_id: String? = null,
    val stream: Boolean = true
)

@Serializable
data class TaskProgressPayload(
    val task_id: String,
    val status: String,
    val progress: Float,
    val message: String? = null,
    val current_step: String? = null
)

@Serializable
data class TaskCompletedPayload(
    val task_id: String,
    val status: String,
    val result: String? = null,
    val error: String? = null
)

@Serializable
data class ApprovalRequiredPayload(
    val task_id: String,
    val subtask_id: String,
    val description: String,
    val risk_level: String,
    val tool_name: String? = null,
    val arguments: Map<String, JsonElement>? = null
)

@Serializable
data class ErrorPayload(
    val message: String? = null,
    val error: String? = null,
    val error_code: String? = null
)

// Approval Models
@Serializable
data class ApproveRequest(
    val task_id: String,
    val subtask_id: String,
    val approved: Boolean,
    val reason: String? = null
)

// Task Models
@Serializable
data class TaskStatus(
    val task_id: String,
    val status: String,
    val progress: Float = 0f,
    val message: String? = null,
    val result: String? = null,
    val error: String? = null,
    val created_at: Long,
    val updated_at: Long
)

// Tool Models
@Serializable
data class ToolManifest(
    val name: String,
    val display_name: String,
    val description: String,
    val category: String,
    val risk_level: String,
    val input_schema: Map<String, JsonElement>,
    val output_schema: Map<String, JsonElement>,
    val required_permissions: List<String>,
    val optional_permissions: List<String>,
    val requires_approval: Boolean,
    val timeout_seconds: Int
)

@Serializable
data class ToolsResponse(
    val tools: List<ToolManifest>
)

// Health & Settings
@Serializable
data class HealthResponse(
    val status: String,
    val version: String,
    val uptime_seconds: Float,
    val components: Map<String, ComponentHealth>
)

@Serializable
data class ComponentHealth(
    val status: String,
    val details: Map<String, JsonElement>? = null
)

@Serializable
data class SettingsResponse(
    val server: ServerSettings,
    val model_providers: List<ProviderSettings>,
    val security: SecuritySettings
)

@Serializable
data class ServerSettings(
    val host: String,
    val port: Int,
    val debug: Boolean
)

@Serializable
data class ProviderSettings(
    val id: String,
    val name: String,
    val type: String,
    val is_primary: Boolean,
    val is_healthy: Boolean
)

@Serializable
data class SecuritySettings(
    val localhost_only: Boolean,
    val require_consent: Boolean,
    val require_approval: Boolean
)

// Voice Models
@Serializable
data class VoiceRecordRequest(
    val duration_seconds: Int = 10,
    val sample_rate: Int = 16000
)

@Serializable
data class VoiceRecordResponse(
    val audio_file: String,
    val duration_seconds: Float
)

@Serializable
data class VoiceTranscribeRequest(
    val audio_file: String,
    val language: String = "en"
)

@Serializable
data class VoiceTranscribeResponse(
    val text: String,
    val language: String
)

@Serializable
data class VoiceSpeakRequest(
    val text: String,
    val voice: String = "en-US-AriaNeural",
    val rate: String = "+0%",
    val volume: String = "+0%",
    val output_file: String? = null
)

@Serializable
data class VoiceSpeakResponse(
    val audio_file: String,
    val duration_estimate: Float
)

@Serializable
data class WakeWordListenRequest(
    val action: String, // "start", "stop", "status"
    val keywords: List<String>? = null
)

@Serializable
data class WakeWordListenResponse(
    val running: Boolean,
    val keywords: List<String>
)

// Conversation History Models
@Serializable
data class ConversationHistoryResponse(
    val conversations: List<ConversationSummary>
)

@Serializable
data class ConversationSummary(
    val id: String,
    val title: String,
    val last_message: String,
    val message_count: Int,
    val updated_at: Long
)

@Serializable
data class ConversationDetail(
    val id: String,
    val messages: List<ChatMessage>,
    val created_at: Long,
    val updated_at: Long
)