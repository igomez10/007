// 001_create_messages.js
// Create the `messages` collection (idempotent) and its indexes.

const collections = db.getCollectionNames();
if (!collections.includes("messages")) {
  db.createCollection("messages");
  print("created collection: messages");
} else {
  print("collection already exists: messages");
}

// Support aggregating/listing a conversation's messages in chronological order.
db.messages.createIndex({ conversation_id: 1, created_at: 1 });
print("ensured index on messages.{conversation_id, created_at}");
