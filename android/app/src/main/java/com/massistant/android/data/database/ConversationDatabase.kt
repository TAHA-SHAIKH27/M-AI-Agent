package com.massistant.android.data.database

import androidx.room.*
import com.massistant.android.data.model.ChatMessage
import kotlinx.serialization.json.Json
import kotlinx.serialization.encodeToString
import kotlinx.serialization.decodeFromString

@Entity(tableName = "conversations")
data class ConversationEntity(
    @PrimaryKey val id: String,
    val title: String,
    val lastMessage: String,
    val messageCount: Int,
    val updatedAt: Long,
    val createdAt: Long
)

@Entity(tableName = "messages")
data class MessageEntity(
    @PrimaryKey(autoGenerate = true) val id: Int = 0,
    val conversationId: String,
    val role: String,
    val content: String,
    val timestamp: Long
) {
    @Ignore
    fun toChatMessage(): ChatMessage {
        return ChatMessage(role = role, content = content, timestamp = timestamp)
    }
}

@Dao
interface ConversationDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertConversation(conversation: ConversationEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertMessages(messages: List<MessageEntity>)

    @Query("SELECT * FROM conversations ORDER BY updatedAt DESC LIMIT :limit OFFSET :offset")
    suspend fun getConversations(limit: Int, offset: Int): List<ConversationEntity>

    @Query("SELECT * FROM conversations WHERE id = :id")
    suspend fun getConversation(id: String): ConversationEntity?

    @Query("SELECT * FROM messages WHERE conversationId = :conversationId ORDER BY timestamp ASC")
    suspend fun getMessages(conversationId: String): List<MessageEntity>

    @Query("DELETE FROM conversations WHERE id = :id")
    suspend fun deleteConversation(id: String)

    @Query("DELETE FROM messages WHERE conversationId = :conversationId")
    suspend fun deleteMessages(conversationId: String)

    @Query("SELECT COUNT(*) FROM conversations")
    suspend fun getConversationCount(): Int
}

@Database(entities = [ConversationEntity::class, MessageEntity::class], version = 1, exportSchema = false)
abstract class ConversationDatabase : RoomDatabase() {
    abstract fun conversationDao(): ConversationDao

    companion object {
        @Volatile private var INSTANCE: ConversationDatabase? = null

        fun getInstance(context: android.content.Context): ConversationDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    ConversationDatabase::class.java,
                    "conversation_database"
                ).fallbackToDestructiveMigration().build()
                INSTANCE = instance
                instance
            }
        }
    }
}