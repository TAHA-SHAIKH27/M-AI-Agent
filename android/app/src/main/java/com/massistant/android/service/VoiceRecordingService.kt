package com.massistant.android.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.massistant.android.R
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.io.File
import java.io.IOException

class VoiceRecordingService : Service() {

    private val CHANNEL_ID = "voice_recording"
    private val NOTIFICATION_ID = 1001
    private var mediaRecorder: MediaRecorder? = null
    private var outputFile: File? = null
    private var isRecording = false

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: return START_NOT_STICKY
        
        when (action) {
            "START_RECORDING" -> startRecording()
            "STOP_RECORDING" -> stopRecording()
        }
        
        return START_STICKY
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Voice Recording",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Active voice recording session"
            }
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }

    private fun startRecording() {
        if (isRecording) return
        
        try {
            outputFile = File(cacheDir, "voice_${System.currentTimeMillis()}.3gp")
            
            mediaRecorder = MediaRecorder().apply {
                setAudioSource(MediaRecorder.AudioSource.MIC)
                setOutputFormat(MediaRecorder.OutputFormat.THREE_GPP)
                setAudioEncoder(MediaRecorder.AudioEncoder.AMR_NB)
                setOutputFile(outputFile!!.absolutePath)
            }
            
            mediaRecorder?.prepare()
            mediaRecorder?.start()
            isRecording = true
            
            showNotification("Recording…", "Tap to stop")
            
        } catch (e: IOException) {
            e.printStackTrace()
            stopSelf()
        }
    }

    private fun stopRecording() {
        if (!isRecording) return
        
        mediaRecorder?.stop()
        mediaRecorder?.release()
        mediaRecorder = null
        isRecording = false
        
        val file = outputFile
        outputFile = null
        
        stopForeground(true)
        stopSelf()
        
        // Send result back via intent/broadcast
        file?.let { sendRecordingResult(it.absolutePath) }
    }

    private fun sendRecordingResult(filePath: String) {
        // Broadcast the result to the UI
        val intent = Intent("VOICE_RECORDING_COMPLETE")
        intent.putExtra("file_path", filePath)
        sendBroadcast(intent)
    }

    private fun showNotification(title: String, text: String) {
        val stopIntent = Intent(this, VoiceRecordingService::class.java).apply {
            action = "STOP_RECORDING"
        }
        val stopPendingIntent = PendingIntent.getService(
            this, 0, stopIntent, 
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_mic)
            .setOngoing(true)
            .addAction(R.drawable.ic_stop, "Stop", stopPendingIntent)
            .build()

        startForeground(NOTIFICATION_ID, notification)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        if (isRecording) {
            stopRecording()
        }
        super.onDestroy()
    }

    companion object {
        fun startRecording(context: Context) {
            val intent = Intent(context, VoiceRecordingService::class.java).apply {
                action = "START_RECORDING"
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }
}