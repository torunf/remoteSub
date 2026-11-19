// SPDX-License-Identifier: AGPL-3.0-only

package com.torunf.remotesub

import android.content.Context
import android.util.Log
import com.google.mlkit.common.model.DownloadConditions
import com.google.mlkit.nl.translate.TranslateLanguage
import com.google.mlkit.nl.translate.Translation
import com.google.mlkit.nl.translate.TranslatorOptions
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

enum class Origin { DICTIONARY, CACHE, OLLAMA, ON_DEVICE }

/**
 * Dictionary first, then TranslateGemma through Ollama on a computer on the home network (the TV
 * can't run the model itself), then ML Kit's offline model when that computer can't be reached.
 */
class Translator(context: Context, private val config: RemoteSubConfig)
{
	companion object
	{
		private const val TAG = "RemoteSub"
		private const val OLLAMA_RETRY_MS = 30_000L

		/** Translation models on an Ollama server, best first. */
		fun ollamaModels(baseUrl: String, timeoutMs: Int = 3000): List<String>
		{
			val conn = URL("$baseUrl/api/tags").openConnection() as HttpURLConnection
			conn.connectTimeout = timeoutMs
			conn.readTimeout = timeoutMs
			try
			{
				if(conn.responseCode != 200)
					throw IllegalStateException("HTTP ${conn.responseCode}")
				val models = JSONObject(conn.inputStream.bufferedReader().readText()).optJSONArray("models")
				val names = (0 until (models?.length() ?: 0)).map { models!!.getJSONObject(it).optString("name") }
				return names.filter { it.startsWith("translategemma") }
					.sortedByDescending { Regex(":(\\d+)b").find(it)?.groupValues?.get(1)?.toIntOrNull() ?: 0 }
			}
			finally
			{
				conn.disconnect()
			}
		}

		fun onDeviceTranslator(source: String, target: String) = Translation.getClient(
			TranslatorOptions.Builder()
				.setSourceLanguage(TranslateLanguage.fromLanguageTag(source) ?: TranslateLanguage.ENGLISH)
				.setTargetLanguage(TranslateLanguage.fromLanguageTag(target) ?: TranslateLanguage.TURKISH)
				.build())
	}

	val dictionaries = DictionaryStore(context)
	private val cache = object: LinkedHashMap<String, String>(256, 0.75f, true)
	{
		override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, String>) = size > 1000
	}
	private val learnedDir = File(context.filesDir, "learned").apply { mkdirs() }
	private var learned: SubtitleDictionary? = null
	private var learnedRaw = JSONObject()
	private var learnedFile: File? = null
	private var ollamaRetryAt = 0L
	private var ollamaModel: String? = null
	private var ollamaModelFor: String? = null

	/** Why Ollama isn't being used right now, for the status line; null when it is. */
	@Volatile var ollamaProblem: String? = null
		private set

	private val onDeviceLock = Mutex()
	private var onDevice: com.google.mlkit.nl.translate.Translator? = null
	private var onDeviceLangs: Pair<String, String>? = null

	suspend fun warmUp() = withContext(Dispatchers.IO) {
		val url = config.ollamaUrl
		if(url.isNotEmpty())
		{
			try
			{
				post(url, JSONObject().put("model", resolveModel(url)).put("keep_alive", "30m"), 120_000)
			}
			catch(e: Exception)
			{
				Log.w(TAG, "warm-up failed: $e")
			}
		}
		runCatching { onDeviceClient() }
	}

	/** The translation and where it came from. */
	suspend fun translate(text: String, profile: Profile): Pair<String, Origin> = withContext(Dispatchers.IO) {
		dictionaries.get(profile.dictionaryUrl)?.lookup(text)?.let { return@withContext it to Origin.DICTIONARY }
		synchronized(cache) { cache[text] }?.let { return@withContext it to Origin.CACHE }
		synchronized(learnedDir) { learnedDictionary().lookup(text) }?.let {
			synchronized(cache) { cache[text] = it }
			return@withContext it to Origin.CACHE
		}

		val url = config.ollamaUrl
		if(url.isNotEmpty() && System.currentTimeMillis() >= ollamaRetryAt)
		{
			try
			{
				val out = ollamaTranslate(url, text)
				ollamaProblem = null
				synchronized(cache) { cache[text] = out }
				learn(text, out)
				return@withContext out to Origin.OLLAMA
			}
			catch(e: Exception)
			{
				Log.w(TAG, "Ollama failed, using on-device translation for ${OLLAMA_RETRY_MS / 1000} s: $e")
				ollamaProblem = e.message ?: e.toString()
				ollamaRetryAt = System.currentTimeMillis() + OLLAMA_RETRY_MS
				ollamaModel = null
			}
		}
		else if(url.isEmpty())
			ollamaProblem = null

		val out = onDeviceClient().translate(text).await()
		synchronized(cache) { cache[text] = out }
		out to Origin.ON_DEVICE
	}

	/**
	 * Every line the model translates, kept on disk per language pair, so it is never sent to the
	 * model again. Same format as the other dictionaries (source line → translation), so the file
	 * can be copied to the desktop app or shared. On-device translations aren't kept: they are
	 * worse, and the model should get a chance at those lines later.
	 */
	private fun learnedDictionary(): SubtitleDictionary
	{
		val file = File(learnedDir, "${config.source}_${config.target}.json")
		if(file != learnedFile || learned == null)
		{
			learnedRaw = runCatching { JSONObject(file.readText()) }.getOrDefault(JSONObject())
			learned = SubtitleDictionary(learnedRaw)
			learnedFile = file
			Log.i(TAG, "learned dictionary ${file.name}: ${learned!!.size} lines")
		}
		return learned!!
	}

	private fun learn(source: String, translation: String) = synchronized(learnedDir) {
		learnedDictionary().put(source, translation)
		learnedRaw.put(source, translation)
		val file = learnedFile ?: return@synchronized
		runCatching {
			val tmp = File(file.path + ".tmp")
			tmp.writeText(learnedRaw.toString(1))
			tmp.renameTo(file)
		}.onFailure { Log.w(TAG, "couldn't save the learned dictionary: $it") }
	}

	/** Lines learned so far for the current language pair. */
	val learnedCount get() = synchronized(learnedDir) { learnedDictionary().size }

	fun close()
	{
		onDevice?.close()
		onDevice = null
	}

	private suspend fun onDeviceClient(): com.google.mlkit.nl.translate.Translator = onDeviceLock.withLock {
		val langs = config.source to config.target
		val existing = onDevice
		if(existing != null && onDeviceLangs == langs)
			return existing
		existing?.close()
		val client = onDeviceTranslator(langs.first, langs.second)
		// Downloads the language model once (about 30 MB); later calls return immediately.
		client.downloadModelIfNeeded(DownloadConditions.Builder().build()).await()
		onDevice = client
		onDeviceLangs = langs
		client
	}

	private fun resolveModel(url: String): String
	{
		config.model.takeIf { it.isNotEmpty() }?.let { return it }
		if(ollamaModel != null && ollamaModelFor == url)
			return ollamaModel!!
		val model = ollamaModels(url).firstOrNull()
			?: throw IllegalStateException("no translategemma model on $url")
		ollamaModel = model
		ollamaModelFor = url
		return model
	}

	private fun ollamaTranslate(url: String, text: String): String
	{
		val model = resolveModel(url)
		val source = config.source
		val target = config.target
		val src = RemoteSubConfig.LANG_NAMES[source] ?: source
		val tgt = RemoteSubConfig.LANG_NAMES[target] ?: target
		// TranslateGemma is trained on this exact prompt shape (note the two blank lines).
		val prompt = "You are a professional $src ($source) to $tgt ($target) translator. " +
			"Your goal is to accurately convey the meaning and nuances of the original $src text " +
			"while adhering to $tgt grammar, vocabulary, and cultural sensitivities.\n" +
			"Produce only the $tgt translation, without any additional explanations or commentary. " +
			"Please translate the following $src text into $tgt:\n\n\n$text"
		// Cap output so a runaway generation can't append invented sentences.
		val maxTokens = 24 + 4 * text.split(Regex("\\s+")).size

		fun run(temperature: Double) = post(url, JSONObject()
			.put("model", model).put("prompt", prompt).put("stream", false).put("keep_alive", "30m")
			.put("options", JSONObject().put("temperature", temperature).put("num_predict", maxTokens)),
			60_000)

		var out = run(0.0)
		// A translation far longer than its source means the model added sentences; retry once.
		if(out.length > 1.8 * text.length + 15)
			out = listOf(out, run(0.4)).minByOrNull { it.length }!!
		return out
	}

	private fun post(url: String, payload: JSONObject, readTimeoutMs: Int): String
	{
		val conn = URL("$url/api/generate").openConnection() as HttpURLConnection
		conn.connectTimeout = 2000
		conn.readTimeout = readTimeoutMs
		conn.requestMethod = "POST"
		conn.doOutput = true
		conn.setRequestProperty("Content-Type", "application/json")
		try
		{
			conn.outputStream.use { it.write(payload.toString().toByteArray(Charsets.UTF_8)) }
			if(conn.responseCode != 200)
			{
				val err = conn.errorStream?.bufferedReader()?.readText() ?: ""
				throw IllegalStateException("HTTP ${conn.responseCode} $err".trim())
			}
			return JSONObject(conn.inputStream.bufferedReader().readText()).optString("response", "").trim()
		}
		finally
		{
			conn.disconnect()
		}
	}
}
