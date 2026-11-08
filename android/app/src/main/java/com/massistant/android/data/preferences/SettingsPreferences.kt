package com.massistant.android.data.preferences

import android.content.Context
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.PreferencesKeys
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.preferencesKey
import androidx.datastore.preferences.rxjava3.RxDataStore
import androidx.datastore.rxjava3.data
import androidx.datastore.rxjava3.updateData
import dagger.hilt.android.qualifiers.ApplicationContext
import io.reactivex.rxjava3.core.Completable
import io.reactivex.rxjava3.core.Single
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class SettingsPreferences @Inject constructor(
    @ApplicationContext private val context: Context
) {
    private val dataStore = RxDataStore(context, "m_companion_settings")

    companion object {
        val SERVER_URL = preferencesKey<String>("server_url")
        val API_KEY = preferencesKey<String>("api_key")
        val AUTO_CONNECT = preferencesKey<Boolean>("auto_connect")
        val VOICE_ENABLED = preferencesKey<Boolean>("voice_enabled")
        val NOTIFICATIONS_ENABLED = preferencesKey<Boolean>("notifications_enabled")
        val THEME_MODE = preferencesKey<Int>("theme_mode") // 0=system, 1=light, 2=dark
    }

    val serverUrl: Single<String> = dataStore.data
        .map { it[SERVER_URL] ?: "http://192.168.1.100:8000" }
        .firstOrError()

    val apiKey: Single<String?> = dataStore.data
        .map { it[API_KEY] }
        .firstOrError()

    val autoConnect: Single<Boolean> = dataStore.data
        .map { it[AUTO_CONNECT] ?: true }
        .firstOrError()

    val voiceEnabled: Single<Boolean> = dataStore.data
        .map { it[VOICE_ENABLED] ?: false }
        .firstOrError()

    val notificationsEnabled: Single<Boolean> = dataStore.data
        .map { it[NOTIFICATIONS_ENABLED] ?: true }
        .firstOrError()

    val themeMode: Single<Int> = dataStore.data
        .map { it[THEME_MODE] ?: 0 }
        .firstOrError()

    fun updateServerUrl(url: String): Completable {
        return dataStore.updateData { it[SERVER_URL] = url }.ignoreElement()
    }

    fun updateApiKey(key: String?): Completable {
        return dataStore.updateData { it[API_KEY] = key }.ignoreElement()
    }

    fun updateAutoConnect(enabled: Boolean): Completable {
        return dataStore.updateData { it[AUTO_CONNECT] = enabled }.ignoreElement()
    }

    fun updateVoiceEnabled(enabled: Boolean): Completable {
        return dataStore.updateData { it[VOICE_ENABLED] = enabled }.ignoreElement()
    }

    fun updateNotificationsEnabled(enabled: Boolean): Completable {
        return dataStore.updateData { it[NOTIFICATIONS_ENABLED] = enabled }.ignoreElement()
    }

    fun updateThemeMode(mode: Int): Completable {
        return dataStore.updateData { it[THEME_MODE] = mode }.ignoreElement()
    }
}