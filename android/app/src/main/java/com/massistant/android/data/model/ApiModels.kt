package com.massistant.android.data.model

import kotlinx.serialization.Contextual
import kotlinx.serialization.Serializable
import kotlinx.serialization.descriptors.PolymorphicKind
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.descriptors.SerialKind
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonConfiguration
import kotlinx.serialization.modules.SerializersModule
import kotlinx.serialization.modules.contextual

// Serializers module for handling Any types
object CustomSerializers {
    val module = SerializersModule {
        contextual(Map<String, Any>::class, AnyMapSerializer)
        contextual(Map<String, Any?>::class, AnyNullableMapSerializer)
    }
}

object AnyMapSerializer : kotlinx.serialization.KSerializer<Map<String, Any>> {
    override val descriptor: SerialDescriptor = kotlinx.serialization.descriptors.buildMapSerializer(
        kotlinx.serialization.descriptors.PrimitiveSerialDescriptor("String", SerialKind.STRING),
        kotlinx.serialization.descriptors.AnySerializer
    ).descriptor

    override fun serialize(encoder: Encoder, value: Map<String, Any>) {
        encoder.encodeSerializableValue(value)
    }

    override fun deserialize(decoder: Decoder): Map<String, Any> {
        return decoder.decodeSerializableValue()
    }
}

object AnyNullableMapSerializer : kotlinx.serialization.KSerializer<Map<String, Any?>> {
    override val descriptor: SerialDescriptor = kotlinx.serialization.descriptors.buildMapSerializer(
        kotlinx.serialization.descriptors.PrimitiveSerialDescriptor("String", SerialKind.STRING),
        kotlinx.serialization.descriptors.AnySerializer
    ).descriptor

    override fun serialize(encoder: Encoder, value: Map<String, Any?>) {
        encoder.encodeSerializableValue(value)
    }

    override fun deserialize(decoder: Decoder): Map<String, Any?> {
        return decoder.decodeSerializableValue()
    }
}

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
@PolymorphicSerializer(using = WSMessageSerializer::class)
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

// Custom serializer for WSMessage sealed class
object WSMessageSerializer : kotlinx.serialization.KSerializer<WSMessage> {
    override val descriptor: SerialDescriptor = kotlinx.serialization.descriptors.buildClassSerialDescriptor("WSMessage") {
        element("type", kotlinx.serialization.descriptors.PrimitiveSerialDescriptor("String", SerialKind.STRING))
        element("payload", kotlinx.serialization.descriptors.AnySerializer)
    }.build()

    override fun serialize(encoder: Encoder, value: WSMessage) {
        val output = encoder.beginStructure(descriptor)
        when (value) {
            is WSMessage.Chat -> {
                output.encodeStringElement(descriptor, 0, "chat")
                output.encodeSerializableElement(descriptor, 1, value.payload)
            }
            is WSMessage.TaskProgress -> {
                output.encodeStringElement(descriptor, 0, "task_progress")
                output.encodeSerializableElement(descriptor, 1, value.payload)
            }
            is WSMessage.TaskCompleted -> {
                output.encodeStringElement(descriptor, 0, "task_completed")
                output.encodeSerializableElement(descriptor, 1, value.payload)
            }
            is WSMessage.ApprovalRequired -> {
                output.encodeStringElement(descriptor, 0, "approval_required")
                output.encodeSerializableElement(descriptor, 1, value.payload)
            }
            is WSMessage.Error -> {
                output.encodeStringElement(descriptor, 0, "error")
                output.encodeSerializableElement(descriptor, 1, value.payload)
            }
            is WSMessage.Ping -> {
                output.encodeStringElement(descriptor, 0, "ping")
                output.encodeNullableSerializableElement(descriptor, 1, kotlinx.serialization.descriptors.AnySerializer, null)
            }
            is WSMessage.Pong -> {
                output.encodeStringElement(descriptor, 0, "pong")
                output.encodeNullableSerializableElement(descriptor, 1, kotlinx.serialization.descriptors.AnySerializer, null)
            }
            is WSMessage.Ack -> {
                output.encodeStringElement(descriptor, 0, "ack")
                output.encodeSerializableElement(descriptor, 1, value)
            }
        }
        output.endStructure(descriptor)
    }

    override fun deserialize(decoder: Decoder): WSMessage {
        val input = decoder.beginStructure(descriptor)
        val type = input.decodeStringElement(descriptor, 0)
        val result = when (type) {
            "chat" -> {
                val payload = input.decodeSerializableElement(descriptor, 1, ChatPayload.serializer())
                WSMessage.Chat(payload)
            }
            "task_progress" -> {
                val payload = input.decodeSerializableElement(descriptor, 1, TaskProgressPayload.serializer())
                WSMessage.TaskProgress(payload)
            }
            "task_completed" -> {
                val payload = input.decodeSerializableElement(descriptor, 1, TaskCompletedPayload.serializer())
                WSMessage.TaskCompleted(payload)
            }
            "approval_required" -> {
                val payload = input.decodeSerializableElement(descriptor, 1, ApprovalRequiredPayload.serializer())
                WSMessage.ApprovalRequired(payload)
            }
            "error" -> {
                val payload = input.decodeSerializableElement(descriptor, 1, ErrorPayload.serializer())
                WSMessage.Error(payload)
            }
            "ping" -> WSMessage.Ping
            "pong" -> WSMessage.Pong
            "ack" -> {
                val ack = input.decodeSerializableElement(descriptor, 1, WSMessage.Ack.serializer())
                ack
            }
            else -> throw kotlinx.serialization.SerializationException("Unknown WSMessage type: $type")
        }
        input.endStructure(descriptor)
        return result
    }
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
    @Contextual val tool_name: String? = null,
    @Contextual val arguments: Map<String, Any>? = null
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
    @Contextual val input_schema: Map<String, Any>,
    @Contextual val output_schema: Map<String, Any>,
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
    @Contextual val components: Map<String, ComponentHealth>
)

@Serializable
data class ComponentHealth(
    val status: String,
    @Contextual val details: Map<String, Any>? = null
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