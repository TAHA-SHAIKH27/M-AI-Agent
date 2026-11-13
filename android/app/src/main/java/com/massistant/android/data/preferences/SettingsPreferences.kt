package com.massistant.android.data.preferences

import android.content.Context
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.toMutablePreferences
import androidx.datastore.preferences.rxjava3.RxPreferenceDataStoreBuilder
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
    private val dataStore = RxPreferenceDataStoreBuilder(context, "m_companion_settings").build()

    companion object {
        val SERVER_URL = stringPreferencesKey("server_url")
        val API_KEY = stringPreferencesKey("api_key")
        val AUTO_CONNECT = booleanPreferencesKey("auto_connect")
        val VOICE_ENABLED = booleanPreferencesKey("voice_enabled")
        val NOTIFICATIONS_ENABLED = booleanPreferencesKey("notifications_enabled")
        val THEME_MODE = intPreferencesKey("theme_mode") // 0=system, 1=light, 2=dark
    }

    val serverUrl: Single<String> = dataStore.data
        .map { it[SERVER_URL] ?: "http://192.168.1.100:8080" }
        .firstOrError()

    val apiKey: Single<String> = dataStore.data
        .map { it[API_KEY].orEmpty() }
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
        return dataStore.updateData { preferences -> preferences.toMutablePreferences().apply { this[SERVER_URL] = url } }.ignoreElement()
    }

    fun updateApiKey(key: String?): Completable {
        return dataStore.updateData { preferences ->
            preferences.toMutablePreferences().apply {
                if (key == null) remove(API_KEY) else this[API_KEY] = key
            }
        }.ignoreElement()
    }

    fun updateAutoConnect(enabled: Boolean): Completable {
        return dataStore.updateData { preferences -> preferences.toMutablePreferences().apply { this[AUTO_CONNECT] = enabled } }.ignoreElement()
    }

    fun updateVoiceEnabled(enabled: Boolean): Completable {
        return dataStore.updateData { preferences -> preferences.toMutablePreferences().apply { this[VOICE_ENABLED] = enabled } }.ignoreElement()
    }

    fun updateNotificationsEnabled(enabled: Boolean): Completable {
        return dataStore.updateData { preferences -> preferences.toMutablePreferences().apply { this[NOTIFICATIONS_ENABLED] = enabled } }.ignoreElement()
    }

    fun updateThemeMode(mode: Int): Completable {
        return dataStore.updateData { preferences -> preferences.toMutablePreferences().apply { this[THEME_MODE] = mode } }.ignoreElement()
    }
}