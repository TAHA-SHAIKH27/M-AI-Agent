package com.massistant.android.ui.screens

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.lifecycle.lifecycleScope
import com.massistant.android.data.repository.ChatRepository
import com.massistant.android.ui.theme.MTheme
import dagger.hilt.android.AndroidEntryPoint
import io.reactivex.rxjava3.android.schedulers.AndroidSchedulers
import io.reactivex.rxjava3.core.Single
import io.reactivex.rxjava3.schedulers.Schedulers
import kotlinx.coroutines.launch
import javax.inject.Inject

@AndroidEntryPoint
class MainActivity : ComponentActivity() {

    @Inject
    lateinit var chatRepository: ChatRepository

    @Inject
    lateinit var settingsPreferences: com.massistant.android.data.preferences.SettingsPreferences

    private val viewModel: MainViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MTheme {
                Surface(modifier = androidx.compose.foundation.layout.Modifier.fillMaxSize()) {
                    MainScreen(
                        chatRepository = chatRepository,
                        settingsPreferences = settingsPreferences,
                        viewModel = viewModel
                    )
                }
            }
        }
        
        // Auto-connect if enabled
        lifecycleScope.launch {
            try {
                val autoConnect = settingsPreferences.autoConnect
                    .subscribeOn(Schedulers.io())
                    .observeOn(AndroidSchedulers.mainThread())
                    .blockingGet()
                if (autoConnect) {
                    viewModel.connect()
                }
            } catch (e: Exception) {
                // Ignore, don't auto-connect on error
            }
        }
    }
}

@Composable
fun MainScreen(
    chatRepository: ChatRepository,
    settingsPreferences: com.massistant.android.data.preferences.SettingsPreferences,
    viewModel: MainViewModel
) {
    val currentScreen by viewModel.currentScreen.collectAsState()
    
    when (currentScreen) {
        Screen.Chat -> ChatScreen(
            repository = chatRepository,
            onSettingsClick = { viewModel.navigateTo(Screen.Settings) }
        )
        Screen.Settings -> SettingsScreen(
            settingsPreferences = settingsPreferences,
            onBack = { viewModel.navigateTo(Screen.Chat) }
        )
    }
}

enum class Screen {
    Chat, Settings
}

@dagger.hilt.android.lifecycle.HiltViewModel
class MainViewModel @Inject constructor(
    private val chatRepository: ChatRepository
) : androidx.lifecycle.ViewModel() {

    private val _currentScreen = androidx.compose.runtime.MutableStateFlow(Screen.Chat)
    val currentScreen = _currentScreen.asStateFlow()

    fun navigateTo(screen: Screen) {
        _currentScreen.value = screen
    }

    fun connect() {
        androidx.lifecycle.viewmodel.viewModelScope.launch {
            chatRepository.connect()
        }
    }

    fun disconnect() {
        androidx.lifecycle.viewmodel.viewModelScope.launch {
            chatRepository.disconnect()
        }
    }

    override fun onCleared() {
        super.onCleared()
        disconnect()
    }
}