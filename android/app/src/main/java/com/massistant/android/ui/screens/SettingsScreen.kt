package com.massistant.android.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardOptions
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.massistant.android.data.preferences.SettingsPreferences
import com.massistant.android.ui.theme.MTheme
import io.reactivex.rxjava3.android.schedulers.AndroidSchedulers
import io.reactivex.rxjava3.core.Completable
import io.reactivex.rxjava3.core.Single
import io.reactivex.rxjava3.disposables.CompositeDisposable
import io.reactivex.rxjava3.schedulers.Schedulers
import kotlinx.coroutines.launch

@Composable
fun SettingsScreen(
    settingsPreferences: SettingsPreferences,
    onBack: () -> Unit
) {
    var serverUrl by remember { mutableStateOf("") }
    var apiKey by remember { mutableStateOf("") }
    var autoConnect by remember { mutableStateOf(true) }
    var voiceEnabled by remember { mutableStateOf(false) }
    var notificationsEnabled by remember { mutableStateOf(true) }
    var isLoading by remember { mutableStateOf(false) }
    var errorMessage by remember { mutableStateOf<String?>(null) }
    var successMessage by remember { mutableStateOf<String?>(null) }
    val disposables = remember { CompositeDisposable() }

    // Load current settings
    LaunchedEffect(Unit) {
        disposables.add(
            settingsPreferences.serverUrl
                .subscribeOn(Schedulers.io())
                .observeOn(AndroidSchedulers.mainThread())
                .subscribe { serverUrl = it }
        )
        
        disposables.add(
            settingsPreferences.apiKey
                .subscribeOn(Schedulers.io())
                .observeOn(AndroidSchedulers.mainThread())
                .subscribe { apiKey = it ?: "" }
        )
        
        disposables.add(
            settingsPreferences.autoConnect
                .subscribeOn(Schedulers.io())
                .observeOn(AndroidSchedulers.mainThread())
                .subscribe { autoConnect = it }
        )
        
        disposables.add(
            settingsPreferences.voiceEnabled
                .subscribeOn(Schedulers.io())
                .observeOn(AndroidSchedulers.mainThread())
                .subscribe { voiceEnabled = it }
        )
        
        disposables.add(
            settingsPreferences.notificationsEnabled
                .subscribeOn(Schedulers.io())
                .observeOn(AndroidSchedulers.mainThread())
                .subscribe { notificationsEnabled = it }
        )
    }

    val scope = rememberCoroutineScope()
    
    val saveSettings = {
        isLoading = true
        errorMessage = null
        successMessage = null
        
        scope.launch {
            try {
                settingsPreferences.updateServerUrl(serverUrl)
                    .subscribeOn(Schedulers.io())
                    .blockingAwait()
                
                settingsPreferences.updateApiKey(apiKey.ifBlank { null })
                    .subscribeOn(Schedulers.io())
                    .blockingAwait()
                
                settingsPreferences.updateAutoConnect(autoConnect)
                    .subscribeOn(Schedulers.io())
                    .blockingAwait()
                
                settingsPreferences.updateVoiceEnabled(voiceEnabled)
                    .subscribeOn(Schedulers.io())
                    .blockingAwait()
                
                settingsPreferences.updateNotificationsEnabled(notificationsEnabled)
                    .subscribeOn(Schedulers.io())
                    .blockingAwait()
                
                successMessage = "Settings saved successfully"
            } catch (e: Exception) {
                errorMessage = "Failed to save settings: ${e.message}"
            } finally {
                isLoading = false
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.Default.ArrowBack, contentDescription = "Back") } },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.surface)
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(24.dp)
        ) {
            // Server Configuration
            Card(
                modifier = Modifier.fillMaxWidth(),
                elevation = CardDefaults.cardElevation(defaultElevation = 2.dp)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text("Server Configuration", style = MaterialTheme.typography.titleMedium)
                    androidx.compose.foundation.layout.Spacer(modifier = Modifier.padding(top = 16.dp))
                    
                    OutlinedTextField(
                        value = serverUrl,
                        onValueChange = { serverUrl = it },
                        label = { Text("Server URL") },
                        placeholder = { Text("http://192.168.1.100:8000") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri)
                    )
                    
                    androidx.compose.foundation.layout.Spacer(modifier = Modifier.padding(top = 16.dp))
                    
                    OutlinedTextField(
                        value = apiKey,
                        onValueChange = { apiKey = it },
                        label = { Text("API Key (optional)") },
                        placeholder = { Text("Enter API key if required") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        visualTransformation = androidx.compose.foundation.text.PasswordVisualTransformation()
                    )
                }
            }

            // Preferences
            Card(
                modifier = Modifier.fillMaxWidth(),
                elevation = CardDefaults.cardElevation(defaultElevation = 2.dp)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text("Preferences", style = MaterialTheme.typography.titleMedium)
                    
                    androidx.compose.foundation.layout.Spacer(modifier = Modifier.padding(top = 16.dp))
                    
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column {
                            Text("Auto Connect", style = MaterialTheme.typography.bodyMedium)
                            Text("Automatically connect on app start", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Switch(
                            checked = autoConnect,
                            onCheckedChange = { autoConnect = it },
                            modifier = Modifier.padding(start = 16.dp)
                        )
                    }
                    
                    androidx.compose.foundation.layout.Spacer(modifier = Modifier.padding(top = 16.dp))
                    
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column {
                            Text("Voice Features", style = MaterialTheme.typography.bodyMedium)
                            Text("Enable voice input and output", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Switch(
                            checked = voiceEnabled,
                            onCheckedChange = { voiceEnabled = it },
                            modifier = Modifier.padding(start = 16.dp)
                        )
                    }
                    
                    androidx.compose.foundation.layout.Spacer(modifier = Modifier.padding(top = 16.dp))
                    
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column {
                            Text("Notifications", style = MaterialTheme.typography.bodyMedium)
                            Text("Show notifications for tasks and approvals", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Switch(
                            checked = notificationsEnabled,
                            onCheckedChange = { notificationsEnabled = it },
                            modifier = Modifier.padding(start = 16.dp)
                        )
                    }
                }
            }

            // Messages
            errorMessage?.let { msg ->
                Text(msg, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.fillMaxWidth())
            }
            
            successMessage?.let { msg ->
                Text(msg, color = MaterialTheme.colorScheme.tertiary, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.fillMaxWidth())
            }

            // Save Button
            Button(
                onClick = saveSettings,
                enabled = !isLoading,
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.primary)
            ) {
                if (isLoading) {
                    CircularProgressIndicator(color = MaterialTheme.colorScheme.onPrimary, modifier = Modifier.size(24.dp))
                } else {
                    Text("Save Settings", style = MaterialTheme.typography.labelLarge)
                }
            }

            // Test Connection
            androidx.compose.foundation.layout.Spacer(modifier = Modifier.padding(top = 8.dp))
            OutlinedButton(
                onClick = { /* TODO: Test connection */ },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("Test Connection")
            }
        }
    }
}
