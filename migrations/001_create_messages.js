// 001_create_messages.js
// Create the `messages` collection (idempotent) and its indexes.

const collections = db.getCollectionNames();
if (!collections.includes("messages")) {
  db.createCollection("messages");
  print("created collection: messages");
} else {
  print("collection already exists: messages");
}

db.messages.createIndex({ created_at: 1 });
print("ensured index on messages.created_at");
