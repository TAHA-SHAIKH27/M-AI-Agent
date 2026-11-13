package com.massistant.android.data.api

import android.util.Log
import com.massistant.android.data.model.*
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.ReceiveChannel
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.*
import okhttp3.Request
import java.util.concurrent.TimeUnit

class WebSocketClient(
    private val serverUrl: String,
    private val apiKey: String? = null,
    private val json: Json = Json { 
        ignoreUnknownKeys = true
        useArrayPolymorphism = true
        classDiscriminator = "type"
    }
) {
    private val TAG = "WebSocketClient"
    private var webSocket: WebSocket? = null
    private val client = OkHttpClient.Builder()
        .pingInterval(30, TimeUnit.SECONDS)
        .build()
    
    private val messageChannel = Channel<WSMessage>(Channel.UNLIMITED)
    private val connectionStateChannel = Channel<ConnectionState>(Channel.UNLIMITED)
    private var reconnectJob: Job? = null
    private var isConnecting = false
    private var shouldReconnect = true
    private var reconnectAttempts = 0
    private val maxReconnectAttempts = 5
    private val baseReconnectDelay = 1000L

    sealed class ConnectionState {
        object Connecting : ConnectionState()
        object Connected : ConnectionState()
        data class Disconnected(val reason: String? = null) : ConnectionState()
        data class Error(val message: String) : ConnectionState()
    }

    suspend fun connect(): ReceiveChannel<ConnectionState> {
        if (webSocket != null || isConnecting) {
            return connectionStateChannel
        }
        
        isConnecting = true
        shouldReconnect = true
        reconnectAttempts = 0
        
        connectionStateChannel.send(ConnectionState.Connecting)
        
        val request = Request.Builder()
            .url("$serverUrl/api/v1/ws")
            .addHeader("User-Agent", "M-Android-Companion/1.0")
            .build()
        
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.d(TAG, "WebSocket connected")
                reconnectAttempts = 0
                isConnecting = false
                connectionStateChannel.trySend(ConnectionState.Connected)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val message = json.decodeFromString<WSMessage>(text)
                    messageChannel.trySend(message)
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to parse incoming WebSocket message", e)
                }
            }

            override fun onMessage(webSocket: WebSocket, bytes: okio.ByteString) {
                // Handle binary messages if needed
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Log.d(TAG, "WebSocket closing: $code - $reason")
                webSocket.close(1000, "Client closing")
                this@WebSocketClient.webSocket = null
                connectionStateChannel.trySend(ConnectionState.Disconnected("Server closed: $reason"))
                scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "WebSocket failure", t)
                this@WebSocketClient.webSocket = null
                isConnecting = false
                connectionStateChannel.trySend(ConnectionState.Error(t.message ?: "Unknown error"))
                scheduleReconnect()
            }
        })
        
        return connectionStateChannel
    }

    private fun scheduleReconnect() {
        if (!shouldReconnect) return
        if (reconnectAttempts >= maxReconnectAttempts) {
            Log.w(TAG, "Max reconnect attempts reached")
            connectionStateChannel.trySend(ConnectionState.Error("Max reconnect attempts reached"))
            return
        }
        
        reconnectJob?.cancel()
        reconnectJob = CoroutineScope(Dispatchers.IO).launch {
            val delay = (baseReconnectDelay * Math.pow(2.0, reconnectAttempts.toDouble())).toLong()
            Log.d(TAG, "Reconnecting in ${delay}ms (attempt ${reconnectAttempts + 1})")
            delay(delay)
            reconnectAttempts++
            connect()
        }
    }

    fun sendChat(message: String, conversationId: String? = null) {
        val payload = WSMessage.Chat(
            payload = ChatPayload(
                message = message,
                conversation_id = conversationId,
                stream = true
            )
        )
        sendMessage(payload)
    }

    fun sendApproval(taskId: String, subtaskId: String, approved: Boolean, reason: String? = null) {
        // Server expects WSMessage.ApprovalResponse or similar - send as raw JSON for now
        val jsonString = json.encodeToString(
            mapOf(
                "type" to "approval_response",
                "payload" to mapOf(
                    "task_id" to taskId,
                    "subtask_id" to subtaskId,
                    "approved" to approved,
                    "reason" to reason
                )
            )
        )
        webSocket?.send(jsonString)
    }

    fun sendPing() {
        webSocket?.send(json.encodeToString(WSMessage.Ping()))
    }

    private fun sendMessage(message: WSMessage) {
        val jsonString = json.encodeToString(message)
        webSocket?.send(jsonString)
    }

    val incomingMessages: ReceiveChannel<WSMessage>
        get() = messageChannel

    fun disconnect() {
        shouldReconnect = false
        reconnectJob?.cancel()
        webSocket?.close(1000, "Client disconnect")
        webSocket = null
        connectionStateChannel.trySend(ConnectionState.Disconnected("Client disconnected"))
    }

    val isConnected: Boolean
        get() = webSocket != null
}