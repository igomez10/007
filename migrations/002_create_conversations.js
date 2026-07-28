// 002_create_conversations.js
// Create the `conversations` collection holding summarized titles, keyed by
// conversation_id (one document per conversation).

const collections = db.getCollectionNames();
if (!collections.includes("conversations")) {
  db.createCollection("conversations");
  print("created collection: conversations");
} else {
  print("collection already exists: conversations");
}

db.conversations.createIndex({ conversation_id: 1 }, { unique: true });
print("ensured unique index on conversations.conversation_id");
