package com.massistant.android.ui.screens

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.viewModelScope
import com.massistant.android.data.repository.ChatRepository
import com.massistant.android.data.preferences.SettingsPreferences
import com.massistant.android.ui.theme.MTheme
import dagger.hilt.android.AndroidEntryPoint
import dagger.hilt.android.lifecycle.HiltViewModel
import io.reactivex.rxjava3.android.schedulers.AndroidSchedulers
import io.reactivex.rxjava3.schedulers.Schedulers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@AndroidEntryPoint
class MainActivity : ComponentActivity() {
    @Inject lateinit var chatRepository: ChatRepository
    @Inject lateinit var settingsPreferences: SettingsPreferences
    private val viewModel: MainViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MTheme {
                Surface(modifier = androidx.compose.ui.Modifier.fillMaxSize()) {
                    MainScreen(chatRepository, settingsPreferences, viewModel)
                }
            }
        }
        lifecycleScope.launch {
            try {
                chatRepository.initialize()
                val autoConnect = settingsPreferences.autoConnect
                    .subscribeOn(Schedulers.io())
                    .observeOn(AndroidSchedulers.mainThread())
                    .blockingGet()
                if (autoConnect) viewModel.connect()
            } catch (_: Exception) {
                // Keep the app usable when settings cannot be read.
            }
        }
    }
}

@Composable
fun MainScreen(
    chatRepository: ChatRepository,
    settingsPreferences: SettingsPreferences,
    viewModel: MainViewModel
) {
    val currentScreen by viewModel.currentScreen.collectAsState()
    when (currentScreen) {
        Screen.Chat -> ChatScreen(chatRepository) { viewModel.navigateTo(Screen.Settings) }
        Screen.Settings -> SettingsScreen(settingsPreferences) { viewModel.navigateTo(Screen.Chat) }
    }
}

enum class Screen { Chat, Settings }

@HiltViewModel
class MainViewModel @Inject constructor(
    private val chatRepository: ChatRepository
) : ViewModel() {
    private val _currentScreen = MutableStateFlow(Screen.Chat)
    val currentScreen = _currentScreen.asStateFlow()

    fun navigateTo(screen: Screen) { _currentScreen.value = screen }
    fun connect() { viewModelScope.launch { chatRepository.connect() } }
    fun disconnect() { viewModelScope.launch { chatRepository.disconnect() } }

    override fun onCleared() {
        disconnect()
        super.onCleared()
    }
}
