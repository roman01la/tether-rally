#!/usr/bin/env node
/**
 * Token Generator for RC Car Access
 *
 * Usage:
 *   TOKEN_SECRET="your-secret" node generate-token.js [duration_minutes]
 *
 * Examples:
 *   TOKEN_SECRET="my-secret" node generate-token.js        # 60 min (default)
 *   TOKEN_SECRET="my-secret" node generate-token.js 30     # 30 min
 *   TOKEN_SECRET="my-secret" node generate-token.js 1440   # 24 hours
 */

const crypto = require("crypto");

// Get secret from environment variable
const SECRET_KEY = process.env.TOKEN_SECRET;

if (!SECRET_KEY) {
  console.error("\n❌ Error: TOKEN_SECRET environment variable is not set\n");
  console.error(
    'Usage: TOKEN_SECRET="your-secret" node generate-token.js [minutes]\n'
  );
  process.exit(1);
}

function generateToken(durationMinutes = 60) {
  const expiryTime = Math.floor(Date.now() / 1000) + durationMinutes * 60;
  const expiryHex = expiryTime.toString(16).padStart(8, "0");

  const hmac = crypto.createHmac("sha256", SECRET_KEY);
  hmac.update(expiryHex);
  const signature = hmac.digest("hex").substring(0, 16); // First 16 chars (8 bytes)

  const token = `${expiryHex}${signature}`;
  return token;
}

// Parse command line args
const durationMinutes = parseInt(process.argv[2]) || 60;
const token = generateToken(durationMinutes);

const expiryDate = new Date(Date.now() + durationMinutes * 60 * 1000);

console.log("\n🔑 RC Car Access Token Generated\n");
console.log(`Token:    ${token}`);
console.log(`Valid:    ${durationMinutes} minutes`);
console.log(`Expires:  ${expiryDate.toLocaleString()}\n`);
