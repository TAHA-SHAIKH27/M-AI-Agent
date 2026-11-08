# M Android Companion

Android companion app for the M AI Assistant. Connects to the M backend via REST and WebSocket APIs.

## Features

- **Chat Interface**: Real-time streaming chat with M
- **Voice Input/Output**: Speech-to-text and text-to-speech integration
- **Task Monitoring**: View task progress and status
- **Approval System**: Approve/reject high-risk operations
- **Conversation History**: Persistent local storage of conversations
- **Secure Connection**: Authentication and encrypted communication
- **Settings**: Configure server URL, API key, and preferences

## Architecture

- **Language**: Kotlin
- **UI Framework**: Jetpack Compose (Material 3)
- **Dependency Injection**: Hilt
- **Database**: Room
- **Networking**: Retrofit + OkHttp (REST), OkHttp WebSocket
- **Async**: Kotlin Coroutines + Flow
- **Preferences**: DataStore
- **Media**: Media3 ExoPlayer for playback

## Project Structure

```
app/
├── src/main/java/com/massistant/android/
│   ├── MApp.kt                    # Application class with Hilt
│   ├── MainActivity.kt            # Main entry point
│   ├── data/
│   │   ├── api/                   # API services & WebSocket client
│   │   ├── model/                 # Serialization models
│   │   ├── database/              # Room database
│   │   ├── repository/            # Repository layer
│   │   └── preferences/           # DataStore preferences
│   ├── service/                   # Foreground services
│   └── ui/
│       ├── screens/               # Compose screens
│       ├── components/            # Reusable UI components
│       └── theme/                 # Material 3 theme
```

## Building

### Prerequisites

- Android Studio Hedgehog (2023.1.1) or later
- JDK 17
- Android SDK 34

### Build Commands

```bash
# Debug build
./gradlew assembleDebug

# Release build
./gradlew assembleRelease

# Run tests
./gradlew test

# Install on connected device
./gradlew installDebug
```

## Configuration

The app requires a running M backend server. Configure the server URL in Settings:

1. Open the app
2. Navigate to Settings (drawer menu)
3. Enter the server URL (e.g., `http://192.168.1.100:8000`)
4. Enter API key if required
5. Save and connect

## WebSocket Communication

The app uses WebSocket for real-time features:
- Streaming chat responses
- Task progress updates
- Approval requests
- Voice streaming (future)

## Voice Features

- **Recording**: Foreground service with notification
- **Playback**: ExoPlayer-based foreground service
- **Wake Word**: Porcupine integration (requires API key)

## Security

- All communication over HTTPS/WSS in production
- API keys stored encrypted in DataStore
- Localhost-only restriction enforced by backend
- Consent and approval workflows respected

## Testing

```bash
# Unit tests
./gradlew test

# Instrumented tests (requires device/emulator)
./gradlew connectedAndroidTest
```

## License

Part of the M AI Assistant project.