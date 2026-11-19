// SPDX-License-Identifier: AGPL-3.0-only

package com.torunf.remotesub

import android.content.Context
import android.util.Log
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.math.min

/** Text comparison shared by the dictionary and the subtitle loop; mirrors the desktop app. */
object TextMatch
{
	private val typography = mapOf('‘' to "'", '’' to "'", '“' to "\"", '”' to "\"", '…' to "...")

	fun normalize(text: String): String
	{
		val sb = StringBuilder(text.length)
		for(c in text)
			sb.append(typography[c] ?: c)
		return sb.split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")
	}

	/** Normalized Indel similarity in 0..100, the same score as rapidfuzz's fuzz.ratio. */
	fun ratio(a: String, b: String): Double
	{
		val total = a.length + b.length
		if(total == 0)
			return 100.0
		return 100.0 * 2 * lcs(a, b) / total
	}

	private fun lcs(a: String, b: String): Int
	{
		if(a.isEmpty() || b.isEmpty())
			return 0
		var prev = IntArray(b.length + 1)
		var cur = IntArray(b.length + 1)
		for(i in 1..a.length)
		{
			val ca = a[i - 1]
			for(j in 1..b.length)
				cur[j] = if(ca == b[j - 1]) prev[j - 1] + 1 else maxOf(prev[j], cur[j - 1])
			val t = prev; prev = cur; cur = t
		}
		return prev[b.length]
	}

	/** Same line, allowing for OCR jitter between reads. */
	fun similar(a: String, b: String) = a == b || ratio(a, b) >= 90.0
}

/**
 * A pre-translated subtitle dictionary: a JSON object mapping source lines to translations.
 * Close matches count too, since OCR rarely reads a line perfectly.
 */
class SubtitleDictionary(raw: JSONObject)
{
	companion object
	{
		private const val FUZZY_CUTOFF = 88.0  // below this, distinct lines start to match each other
		private const val MIN_FUZZY_LENGTH = 6 // very short lines ("Or?") match the wrong entry too easily
	}

	private val pairs = HashMap<String, String>()
	private val keys = ArrayList<String>()

	init
	{
		for(k in raw.keys())
		{
			val v = raw.opt(k)
			if(v is String)
				pairs[TextMatch.normalize(k)] = v
		}
		keys.addAll(pairs.keys)
	}

	/** Adds a line, as the learned dictionary does with every new translation. */
	fun put(source: String, translation: String)
	{
		val key = TextMatch.normalize(source)
		if(pairs.put(key, translation) == null)
			keys.add(key)
	}

	val size get() = pairs.size

	fun lookup(text: String): String?
	{
		val line = TextMatch.normalize(text)
		pairs[line]?.let { return it }
		if(line.length < MIN_FUZZY_LENGTH)
			return null
		var best: String? = null
		var bestScore = FUZZY_CUTOFF
		for(key in keys)
		{
			// The best score two lengths allow; skips most of a large dictionary cheaply.
			if(200.0 * min(key.length, line.length) / (key.length + line.length) < bestScore)
				continue
			val score = TextMatch.ratio(line, key)
			if(score >= bestScore)
			{
				best = key
				bestScore = score
			}
		}
		return best?.let { pairs[it] }
	}
}

/**
 * Dictionaries by address: http(s)://… on the home network, a file path, or asset://name for the
 * bundled demo. Network ones are rechecked every 30 seconds (they may grow while you play) and
 * kept on disk, so a dictionary still works when the computer serving it is off.
 */
class DictionaryStore(private val context: Context)
{
	private class Entry(var dictionary: SubtitleDictionary?, var etag: String?, var lastModified: String?, var nextCheck: Long)

	private val entries = HashMap<String, Entry>()
	private val cacheDir = File(context.cacheDir, "dictionaries").apply { mkdirs() }

	/** Blocking; call from a background thread. */
	@Synchronized
	fun get(address: String): SubtitleDictionary?
	{
		val url = address.trim()
		if(url.isEmpty())
			return null
		val now = System.currentTimeMillis()
		val entry = entries.getOrPut(url) { Entry(null, null, null, 0) }
		if(now < entry.nextCheck)
			return entry.dictionary
		entry.nextCheck = now + 30_000
		try
		{
			when
			{
				url.startsWith("asset://") ->
					if(entry.dictionary == null)
						entry.dictionary = parse(context.assets.open(url.removePrefix("asset://")).use { it.readBytes() })
				url.startsWith("http://") || url.startsWith("https://") -> fetch(url, entry)
				else ->
				{
					val file = File(url.removePrefix("file://"))
					val stamp = file.lastModified().toString()
					if(entry.lastModified != stamp)
					{
						entry.dictionary = parse(file.readBytes())
						entry.lastModified = stamp
					}
				}
			}
		}
		catch(e: Exception)
		{
			Log.w("RemoteSub", "dictionary $url unavailable: $e")
			if(entry.dictionary == null)
				entry.dictionary = cacheFile(url).takeIf { it.exists() }?.let { runCatching { parse(it.readBytes()) }.getOrNull() }
		}
		return entry.dictionary
	}

	private fun fetch(url: String, entry: Entry)
	{
		val conn = URL(url).openConnection() as HttpURLConnection
		conn.connectTimeout = 3000
		conn.readTimeout = 15000
		if(entry.dictionary != null)
		{
			entry.etag?.let { conn.setRequestProperty("If-None-Match", it) }
			entry.lastModified?.let { conn.setRequestProperty("If-Modified-Since", it) }
		}
		try
		{
			when(conn.responseCode)
			{
				HttpURLConnection.HTTP_NOT_MODIFIED -> return
				HttpURLConnection.HTTP_OK ->
				{
					val bytes = conn.inputStream.use { it.readBytes() }
					entry.dictionary = parse(bytes)
					entry.etag = conn.getHeaderField("ETag")
					entry.lastModified = conn.getHeaderField("Last-Modified")
					cacheFile(url).writeBytes(bytes)
					Log.i("RemoteSub", "dictionary $url: ${entry.dictionary?.size} lines")
				}
				else -> throw IllegalStateException("HTTP ${conn.responseCode}")
			}
		}
		finally
		{
			conn.disconnect()
		}
	}

	private fun parse(bytes: ByteArray) = SubtitleDictionary(JSONObject(String(bytes, Charsets.UTF_8)))

	private fun cacheFile(url: String) = File(cacheDir, Integer.toHexString(url.hashCode()) + ".json")
}
