package com.massistant.android.data.repository

import com.massistant.android.data.api.ApiService
import com.massistant.android.data.api.WebSocketClient
import com.massistant.android.data.database.ConversationDatabase
import com.massistant.android.data.database.ConversationEntity
import com.massistant.android.data.database.MessageEntity
import com.massistant.android.data.model.*
import com.massistant.android.data.preferences.SettingsPreferences
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.ReceiveChannel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Singleton
import kotlin.Result
import retrofit2.Response

@Singleton
class ChatRepository(
    private val apiService: ApiService,
    private val webSocketClient: WebSocketClient,
    private val settingsPreferences: SettingsPreferences,
    private val database: ConversationDatabase
) {
    private val _connectionState = MutableStateFlow<WebSocketClient.ConnectionState>(WebSocketClient.ConnectionState.Disconnected())
    val connectionState = _connectionState.asStateFlow()

    private val _messages = MutableStateFlow<List<ChatMessage>>(emptyList())
    val messages = _messages.asStateFlow()

    private val _currentConversationId = MutableStateFlow<String?>(null)
    val currentConversationId = _currentConversationId.asStateFlow()

    private var wsJob: kotlinx.coroutines.Job? = null
    private var pingJob: kotlinx.coroutines.Job? = null

    suspend fun initialize() {
        // Load last conversation or create new
        val conversations = database.conversationDao().getConversations(1, 0)
        if (conversations.isNotEmpty()) {
            _currentConversationId.value = conversations[0].id
            loadMessages(conversations[0].id)
        }
    }

    private suspend fun loadMessages(conversationId: String) {
        val messages = database.conversationDao().getMessages(conversationId)
            .map { it.toChatMessage() }
        _messages.value = messages
    }

    suspend fun connect(): WebSocketClient.ConnectionState {
        val stateChannel = webSocketClient.connect()
        
        wsJob = CoroutineScope(Dispatchers.IO).launch {
            for (state in stateChannel) {
                _connectionState.value = state
                if (state is WebSocketClient.ConnectionState.Connected) {
                    startPing()
                    // Load conversation history
                    loadConversationHistory()
                }
            }
        }
        
        // Return initial state
        return _connectionState.value
    }

    private fun startPing() {
        pingJob?.cancel()
        pingJob = CoroutineScope(Dispatchers.IO).launch {
            while (webSocketClient.isConnected) {
                delay(30000)
                if (webSocketClient.isConnected) {
                    webSocketClient.sendPing()
                }
            }
        }
    }

    suspend fun disconnect() {
        wsJob?.cancel()
        pingJob?.cancel()
        webSocketClient.disconnect()
        _connectionState.value = WebSocketClient.ConnectionState.Disconnected("Disconnected")
    }

    suspend fun sendMessage(message: String): Result<ChatResponse> {
        val conversationId = _currentConversationId.value
        return try {
            if (webSocketClient.isConnected) {
                // Send via WebSocket for streaming
                saveMessage(message, "user")
                webSocketClient.sendChat(message, conversationId)
                Result.success(ChatResponse(
                    content = "",
                    conversation_id = conversationId ?: "",
                    success = true
                ))
            } else {
                // Fallback to REST
                val response = apiService.sendChat(ChatRequest(
                    message = message,
                    conversation_id = conversationId,
                    stream = false
                ))
                if (response.isSuccessful) {
                    val body = response.body()
                    if (body != null) {
                        if (body.conversation_id.isNotBlank()) _currentConversationId.value = body.conversation_id
                        saveMessage(message, "user")
                        saveMessage(body.content, "assistant")
                        updateConversation(body.conversation_id, message)
                        Result.success(body)
                    } else {
                        Result.failure(Exception("Empty response body"))
                    }
                } else {
                    Result.failure(Exception("HTTP ${response.code()}"))
                }
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    private fun saveMessage(content: String, role: String) {
        val conversationId = _currentConversationId.value ?: "conv_\${System.currentTimeMillis()}".also {
            _currentConversationId.value = it
        }
        val message = ChatMessage(role = role, content = content)
        _messages.value = _messages.value + message

        // Ensure a conversation exists before persisting its first message.
        CoroutineScope(Dispatchers.IO).launch {
            val dao = database.conversationDao()
            if (dao.getConversation(conversationId) == null) {
                dao.insertConversation(
                    ConversationEntity(
                        id = conversationId,
                        title = content.take(50).ifBlank { "New Conversation" },
                        lastMessage = content,
                        messageCount = 0,
                        updatedAt = message.timestamp,
                        createdAt = message.timestamp
                    )
                )
            }
            dao.insertMessages(
                listOf(
                    MessageEntity(
                        conversationId = conversationId,
                        role = role,
                        content = content,
                        timestamp = message.timestamp
                    )
                )
            )
        }
    }

    private fun updateConversation(conversationId: String, lastMessage: String) {
        CoroutineScope(Dispatchers.IO).launch {
            val conversation = database.conversationDao().getConversation(conversationId)
            val entity = ConversationEntity(
                id = conversationId,
                title = lastMessage.take(50),
                lastMessage = lastMessage,
                messageCount = (conversation?.messageCount ?: 0) + 1,
                updatedAt = System.currentTimeMillis(),
                createdAt = conversation?.createdAt ?: System.currentTimeMillis()
            )
            database.conversationDao().insertConversation(entity)
        }
    }

    private fun loadConversationHistory() {
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val response = apiService.getConversations(20, 0)
                if (response.isSuccessful) {
                    response.body()?.conversations?.let { conversations ->
                    for (conv in conversations) {
                        val entity = ConversationEntity(
                            id = conv.id,
                            title = conv.title,
                            lastMessage = conv.last_message,
                            messageCount = conv.message_count,
                            updatedAt = conv.updated_at,
                            createdAt = conv.updated_at // approximate
                        )
                        database.conversationDao().insertConversation(entity)
                    }
                    }
                }
            } catch (e: Exception) {
                // Ignore - local DB will be used
            }
        }
    }

    suspend fun switchConversation(conversationId: String) {
        _currentConversationId.value = conversationId
        loadMessages(conversationId)
    }

    suspend fun createNewConversation(): String {
        val newId = "conv_${System.currentTimeMillis()}"
        _currentConversationId.value = newId
        _messages.value = emptyList()
        
        val entity = ConversationEntity(
            id = newId,
            title = "New Conversation",
            lastMessage = "",
            messageCount = 0,
            updatedAt = System.currentTimeMillis(),
            createdAt = System.currentTimeMillis()
        )
        database.conversationDao().insertConversation(entity)
        
        return newId
    }

    // Handle incoming WebSocket messages
    suspend fun handleIncomingMessages(
        onMessage: (ChatMessage) -> Unit,
        onTaskProgress: (TaskProgressPayload) -> Unit,
        onTaskCompleted: (TaskCompletedPayload) -> Unit,
        onApprovalRequired: (ApprovalRequiredPayload) -> Unit
    ) {
        CoroutineScope(Dispatchers.IO).launch {
            for (message in webSocketClient.incomingMessages) {
                when (message) {
                    is WSMessage.Chat -> {
                        val chatMsg = ChatMessage(role = "assistant", content = message.payload.message)
                        saveMessage(chatMsg.content, "assistant")
                        onMessage(chatMsg)
                    }
                    is WSMessage.TaskProgress -> {
                        onTaskProgress(message.payload)
                    }
                    is WSMessage.TaskCompleted -> {
                        onTaskCompleted(message.payload)
                    }
                    is WSMessage.ApprovalRequired -> {
                        onApprovalRequired(message.payload)
                    }
                    is WSMessage.Error -> {
                        // Handle error
                    }
                    else -> {}
                }
            }
        }
    }

    fun sendApproval(taskId: String, subtaskId: String, approved: Boolean, reason: String? = null) {
        webSocketClient.sendApproval(taskId, subtaskId, approved, reason)
    }

    // Voice methods
    suspend fun voiceRecord(duration: Int = 10): Result<VoiceRecordResponse> {
        return try {
            val response = apiService.voiceRecord(VoiceRecordRequest(duration_seconds = duration))
            if (response.isSuccessful) Result.success(response.body()!!)
            else Result.failure(Exception("HTTP ${response.code()}"))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun voiceTranscribe(audioFile: String): Result<VoiceTranscribeResponse> {
        return try {
            val response = apiService.voiceTranscribe(VoiceTranscribeRequest(audio_file = audioFile))
            if (response.isSuccessful) Result.success(response.body()!!)
            else Result.failure(Exception("HTTP ${response.code()}"))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun voiceSpeak(text: String, voice: String = "en-US-AriaNeural"): Result<VoiceSpeakResponse> {
        return try {
            val response = apiService.voiceSpeak(VoiceSpeakRequest(
                text = text,
                voice = voice
            ))
            if (response.isSuccessful) Result.success(response.body()!!)
            else Result.failure(Exception("HTTP ${response.code()}"))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}