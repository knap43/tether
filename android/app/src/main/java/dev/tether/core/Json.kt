package dev.tether.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.longOrNull

fun toJson(v: Any?): JsonElement = when (v) {
    null -> JsonNull
    is JsonElement -> v
    is String -> JsonPrimitive(v)
    is Number -> JsonPrimitive(v)
    is Boolean -> JsonPrimitive(v)
    is Map<*, *> -> JsonObject(v.entries.associate { it.key.toString() to toJson(it.value) })
    is Iterable<*> -> JsonArray(v.map { toJson(it) })
    is Array<*> -> JsonArray(v.map { toJson(it) })
    else -> throw IllegalArgumentException("cannot encode ${v::class.java}")
}

fun jobj(vararg pairs: Pair<String, Any?>): JsonObject =
    JsonObject(pairs.associate { it.first to toJson(it.second) })

fun JsonObject.with(vararg pairs: Pair<String, Any?>): JsonObject =
    JsonObject(this + pairs.associate { it.first to toJson(it.second) })

fun JsonObject.str(k: String): String? = (this[k] as? JsonPrimitive)?.takeIf { it.isString }?.content

fun JsonObject.long(k: String): Long? = (this[k] as? JsonPrimitive)?.takeIf { !it.isString }?.longOrNull

fun JsonObject.int(k: String): Int? = long(k)?.takeIf { it in Int.MIN_VALUE..Int.MAX_VALUE }?.toInt()

fun JsonObject.bool(k: String): Boolean? = (this[k] as? JsonPrimitive)?.takeIf { !it.isString }?.booleanOrNull

fun JsonObject.obj(k: String): JsonObject? = this[k] as? JsonObject

fun JsonObject.arr(k: String): JsonArray? = this[k] as? JsonArray

fun JsonElement.asObj(): JsonObject? = this as? JsonObject

fun JsonElement.asStr(): String? = (this as? JsonPrimitive)?.takeIf { it.isString }?.content

fun JsonElement.asLong(): Long? = (this as? JsonPrimitive)?.takeIf { !it.isString }?.longOrNull

fun parseObj(s: String): JsonObject = Json.parseToJsonElement(s).jsonObject
