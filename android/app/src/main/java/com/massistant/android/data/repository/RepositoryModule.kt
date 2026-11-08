package com.massistant.android.data.repository

import android.content.Context
import com.massistant.android.data.api.ApiService
import com.massistant.android.data.api.WebSocketClient
import com.massistant.android.data.database.ConversationDatabase
import com.massistant.android.data.preferences.SettingsPreferences
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object RepositoryModule {

    @Provides
    @Singleton
    fun provideConversationDatabase(@ApplicationContext context: Context): ConversationDatabase {
        return ConversationDatabase.getInstance(context)
    }

    @Provides
    @Singleton
    fun provideChatRepository(
        apiService: ApiService,
        webSocketClient: WebSocketClient,
        settingsPreferences: SettingsPreferences,
        database: ConversationDatabase
    ): ChatRepository {
        return ChatRepository(apiService, webSocketClient, settingsPreferences, database)
    }
}