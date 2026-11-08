package com.massistant.android.data.api

import com.massistant.android.data.model.*
import kotlinx.coroutines.flow.Flow
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.*

interface ApiService {

    // Health
    @GET("/health")
    suspend fun getHealth(): Response<HealthResponse>

    @GET("/health/ready")
    suspend fun getReadiness(): Response<HealthResponse>

    // Settings
    @GET("/settings")
    suspend fun getSettings(): Response<SettingsResponse>

    // Providers
    @GET("/providers")
    suspend fun getProviders(): Response<List<ProviderSettings>>

    // Chat
    @POST("/chat")
    suspend fun sendChat(@Body request: ChatRequest): Response<ChatResponse>

    @POST("/chat/stream")
    suspend fun sendChatStream(@Body request: ChatRequest): Response<ResponseBody>

    // Tools
    @GET("/tools")
    suspend fun getTools(
        @Query("enabled_only") enabledOnly: Boolean = true,
        @Query("risk_level") riskLevel: String? = null,
        @Query("category") category: String? = null
    ): Response<ToolsResponse>

    @GET("/tools/{tool_name}")
    suspend fun getTool(@Path("tool_name") toolName: String): Response<ToolManifest>

    // Memory
    @GET("/memory/search")
    suspend fun searchMemory(
        @Query("query") query: String,
        @Query("tier") tier: String? = null,
        @Query("limit") limit: Int = 10,
        @Query("offset") offset: Int = 0
    ): Response<Any>

    @GET("/memory/{entry_id}")
    suspend fun getMemoryEntry(@Path("entry_id") entryId: String): Response<Any>

    @GET("/memory/conversations")
    suspend fun getConversations(
        @Query("limit") limit: Int = 20,
        @Query("offset") offset: Int = 0
    ): Response<ConversationHistoryResponse>

    @GET("/memory/conversations/{conversation_id}")
    suspend fun getConversationDetail(@Path("conversation_id") conversationId: String): Response<ConversationDetail>

    // Voice Tools
    @POST("/tools/voice_record")
    suspend fun voiceRecord(@Body request: VoiceRecordRequest): Response<VoiceRecordResponse>

    @POST("/tools/voice_transcribe")
    suspend fun voiceTranscribe(@Body request: VoiceTranscribeRequest): Response<VoiceTranscribeResponse>

    @POST("/tools/voice_speak")
    suspend fun voiceSpeak(@Body request: VoiceSpeakRequest): Response<VoiceSpeakResponse>

    @POST("/tools/wake_word_listen")
    suspend fun wakeWordListen(@Body request: WakeWordListenRequest): Response<WakeWordListenResponse>

    // Windows Automation Tools
    @POST("/tools/app_launch")
    suspend fun appLaunch(@Body request: Map<String, Any>): Response<Any>

    @POST("/tools/app_control")
    suspend fun appControl(@Body request: Map<String, Any>): Response<Any>

    @POST("/tools/powershell")
    suspend fun powershell(@Body request: Map<String, Any>): Response<Any>

    @POST("/tools/process_list")
    suspend fun processList(@Body request: Map<String, Any>): Response<Any>

    @POST("/tools/window_list")
    suspend fun windowList(@Body request: Map<String, Any>): Response<Any>

    @POST("/tools/shortcut_create")
    suspend fun shortcutCreate(@Body request: Map<String, Any>): Response<Any>

    @POST("/tools/system_command")
    suspend fun systemCommand(@Body request: Map<String, Any>): Response<Any>

    // Task Management
    @POST("/tasks")
    suspend fun createTask(@Body request: Map<String, Any>): Response<Any>

    @GET("/tasks/{task_id}")
    suspend fun getTaskStatus(@Path("task_id") taskId: String): Response<TaskStatus>

    @POST("/tasks/{task_id}/cancel")
    suspend fun cancelTask(@Path("task_id") taskId: String): Response<Any>

    @POST("/tasks/{task_id}/approve")
    suspend fun approveTask(@Path("task_id") taskId: String, @Body request: ApproveRequest): Response<Any>
}