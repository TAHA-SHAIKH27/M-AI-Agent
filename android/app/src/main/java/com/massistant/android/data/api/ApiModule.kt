package com.massistant.android.data.api

import com.google.gson.Gson
import com.google.gson.GsonBuilder
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Qualifier
import javax.inject.Singleton
import kotlin.annotation.AnnotationRetention
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

@Module
@InstallIn(SingletonComponent::class)
object ApiModule {

    @Provides
    @Singleton
    fun provideOkHttpClient(): OkHttpClient {
        val logging = HttpLoggingInterceptor()
        logging.level = if (true) HttpLoggingInterceptor.Level.BASIC else HttpLoggingInterceptor.Level.NONE

        return OkHttpClient.Builder()
            .addInterceptor(logging)
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .build()
    }

    @Provides
    @Singleton
    fun provideGson(): Gson {
        return GsonBuilder()
            .setLenient()
            .create()
    }

    @Provides
    @ServerUrl
    fun provideServerUrl(settingsPreferences: com.massistant.android.data.preferences.SettingsPreferences): String =
        settingsPreferences.serverUrl.blockingGet().trimEnd('/')

    @Provides
    @ApiKey
    fun provideApiKey(settingsPreferences: com.massistant.android.data.preferences.SettingsPreferences): String =
        settingsPreferences.apiKey.blockingGet().orEmpty()

    @Provides
    @Singleton
    fun provideRetrofit(
        okHttpClient: OkHttpClient,
        gson: Gson,
        @ServerUrl baseUrl: String
    ): Retrofit {
        return Retrofit.Builder()
            .baseUrl("$baseUrl/api/v1/")
            .client(okHttpClient)
            .addConverterFactory(GsonConverterFactory.create(gson))
            .build()
    }

    @Provides
    @Singleton
    fun provideApiService(retrofit: Retrofit): ApiService {
        return retrofit.create(ApiService::class.java)
    }

    @Provides
    @Singleton
    fun provideWebSocketClient(
        okHttpClient: OkHttpClient,
        @ServerUrl baseUrl: String,
        @ApiKey apiKey: String
    ): WebSocketClient {
        // Convert HTTP URL to WebSocket URL
        val wsUrl = baseUrl.replace("http://", "ws://").replace("https://", "wss://")
        return WebSocketClient(wsUrl, apiKey.takeIf { it.isNotBlank() })
    }
}

@Qualifier
@Retention(AnnotationRetention.BINARY)
annotation class ServerUrl

@Qualifier
@Retention(AnnotationRetention.BINARY)
annotation class ApiKey