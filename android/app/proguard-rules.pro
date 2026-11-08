# Add project specific ProGuard rules here.
# You can control the set of applied configuration files using the
# proguardFiles setting in build.gradle.
#
# For more details, see
#   http://developer.android.com/guide/developing/tools/proguard.html

# If your project uses WebView with JS, uncomment the following
# and specify the fully qualified class name to the JavaScript interface
# class:
#-keepclassmembers class fqcn.of.javascript.interface.for.webview {
#   public *;
#}

# Uncomment this to preserve the line number information for
# debugging stack traces.
#-keepattributes SourceFile,LineNumberTable

# If you keep the line number information, uncomment this to
# hide the original source file name.
#-renamesourcefileattribute SourceFile

# Retrofit
-keep class retrofit2.** { *; }
-keepattributes Signature
-keepattributes Exceptions

# OkHttp
-keep class okhttp3.** { *; }
-keepattributes Signature

# Gson
-keep class com.google.gson.** { *; }

# Kotlinx Serialization
-keep class kotlinx.serialization.** { *; }

# Room
-keep class androidx.room.** { *; }

# Hilt
-keep class dagger.hilt.** { *; }

# Media3
-keep class androidx.media3.** { *; }

# Coroutines
-keep class kotlinx.coroutines.** { *; }

# DataStore
-keep class androidx.datastore.** { *; }