package com.massistant.android.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.MediaPlayer
import android.net.Uri
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.common.MediaItem
import androidx.media3.exoplayer.Player
import com.massistant.android.R
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers

class VoicePlaybackService : Service() {

    private val CHANNEL_ID = "voice_playback"
    private val NOTIFICATION_ID = 1002
    private var exoPlayer: ExoPlayer? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        exoPlayer = ExoPlayer.Builder(this).build()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: return START_NOT_STICKY
        
        when (action) {
            "PLAY" -> {
                val filePath = intent.getStringExtra("file_path") ?: return START_NOT_STICKY
                playAudio(filePath)
            }
            "STOP" -> stopPlayback()
            "PAUSE" -> exoPlayer?.pause()
            "RESUME" -> exoPlayer?.playWhenReady = true
        }
        
        return START_STICKY
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Voice Playback",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Playing voice response"
            }
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }

    private fun playAudio(filePath: String) {
        try {
            val mediaItem = MediaItem.fromUri(filePath)
            exoPlayer?.setMediaItem(mediaItem)
            exoPlayer?.prepare()
            exoPlayer?.playWhenReady = true
            
            showNotification("Playing…", filePath)
            
            exoPlayer?.addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(playbackState: Int) {
                    if (playbackState == Player.STATE_ENDED) {
                        stopSelf()
                    }
                }
            })
        } catch (e: Exception) {
            e.printStackTrace()
            stopSelf()
        }
    }

    private fun stopPlayback() {
        exoPlayer?.stop()
        exoPlayer?.release()
        exoPlayer = null
        stopForeground(true)
        stopSelf()
    }

    private fun showNotification(title: String, text: String) {
        val stopIntent = Intent(this, VoicePlaybackService::class.java).apply {
            action = "STOP"
        }
        val stopPendingIntent = PendingIntent.getService(
            this, 0, stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val pauseIntent = Intent(this, VoicePlaybackService::class.java).apply {
            action = "PAUSE"
        }
        val pausePendingIntent = PendingIntent.getService(
            this, 0, pauseIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_volume_up)
            .setOngoing(true)
            .addAction(R.drawable.ic_pause, "Pause", pausePendingIntent)
            .addAction(R.drawable.ic_stop, "Stop", stopPendingIntent)
            .build()

        startForeground(NOTIFICATION_ID, notification)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        exoPlayer?.release()
        exoPlayer = null
        super.onDestroy()
    }

    companion object {
        fun playAudio(context: Context, filePath: String) {
            val intent = Intent(context, VoicePlaybackService::class.java).apply {
                action = "PLAY"
                putExtra("file_path", filePath)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }
}