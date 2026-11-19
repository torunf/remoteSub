// SPDX-License-Identifier: AGPL-3.0-only

package com.torunf.remotesub

import android.content.Context
import com.metallic.chiaki.R
import org.json.JSONArray
import org.json.JSONObject

/** The subtitle area, as fractions of the video picture (so it survives any screen or stream size). */
data class Region(val x: Float, val y: Float, val w: Float, val h: Float)
{
	fun toJson() = JSONArray().put(x.toDouble()).put(y.toDouble()).put(w.toDouble()).put(h.toDouble())

	companion object
	{
		val DEFAULT = Region(0.1f, 0.78f, 0.8f, 0.16f)

		fun fromJson(a: JSONArray?): Region? =
			if(a == null || a.length() != 4) null
			else Region(a.getDouble(0).toFloat(), a.getDouble(1).toFloat(), a.getDouble(2).toFloat(), a.getDouble(3).toFloat())
	}
}

enum class Placement(val value: String) { COVER("cover"), ABOVE("above"), MATCH("match") }
enum class Background(val value: String) { BLUR("blur"), DIM("dim") }

/** Per-game settings, the same ones the desktop app keeps per profile. */
data class Profile(
	val region: Region? = null,
	val fontSize: Int = 20,
	val placement: Placement = Placement.COVER,
	val background: Background = Background.BLUR,
	val dimPercent: Int = 55,
	val dictionaryUrl: String = "")
{
	fun toJson(): JSONObject = JSONObject()
		.put("region", region?.toJson() ?: JSONObject.NULL)
		.put("font_size", fontSize)
		.put("placement", placement.value)
		.put("background", background.value)
		.put("dim_percent", dimPercent)
		.put("dictionary_url", dictionaryUrl)

	companion object
	{
		fun fromJson(o: JSONObject) = Profile(
			region = Region.fromJson(o.optJSONArray("region")),
			fontSize = o.optInt("font_size", 20),
			placement = Placement.values().firstOrNull { it.value == o.optString("placement") } ?: Placement.COVER,
			background = Background.values().firstOrNull { it.value == o.optString("background") } ?: Background.BLUR,
			dimPercent = o.optInt("dim_percent", 55),
			dictionaryUrl = o.optString("dictionary_url", ""))
	}
}

/**
 * RemoteSub's settings, in their own SharedPreferences file (the settings screen edits the same keys).
 *
 * fixedProfile: use only this profile (the demo keeps its own so it never changes a game's area).
 */
class RemoteSubConfig(context: Context, private val fixedProfile: String? = null)
{
	companion object
	{
		const val PREFS_NAME = "remotesub"
		const val KEY_OLLAMA_URL = "ollama_url"
		const val KEY_MODEL = "ollama_model"
		const val KEY_SOURCE = "source_lang"
		const val KEY_TARGET = "target_lang"
		const val KEY_TRANSLATE_ON_START = "translate_on_start"
		private const val KEY_PROFILES = "profiles"
		private const val KEY_LAST_PROFILE = "last_profile"

		/** Languages ML Kit's Latin text recognizer can read. */
		val SOURCE_LANGUAGES = listOf("en", "de", "fr", "es", "it")
		val TARGET_LANGUAGES = listOf("tr", "en", "de", "fr", "es", "it", "ja", "ko", "ru", "zh")
		val LANG_NAMES = mapOf("en" to "English", "tr" to "Turkish", "de" to "German", "fr" to "French",
			"es" to "Spanish", "it" to "Italian", "ja" to "Japanese", "ko" to "Korean", "ru" to "Russian", "zh" to "Chinese")
	}

	private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
	private val defaultProfileName = context.getString(R.string.rs_profile_default)

	val ollamaUrl: String get() = normalizeUrl(prefs.getString(KEY_OLLAMA_URL, "") ?: "")
	val model: String get() = (prefs.getString(KEY_MODEL, "") ?: "").trim()
	val source: String get() = prefs.getString(KEY_SOURCE, "en") ?: "en"
	val target: String get() = prefs.getString(KEY_TARGET, "tr") ?: "tr"
	val translateOnStart: Boolean get() = prefs.getBoolean(KEY_TRANSLATE_ON_START, true)

	@Synchronized
	fun profiles(): LinkedHashMap<String, Profile>
	{
		val result = LinkedHashMap<String, Profile>()
		val raw = prefs.getString(KEY_PROFILES, null)
		if(raw != null)
		{
			try
			{
				val o = JSONObject(raw)
				for(name in o.keys())
					result[name] = Profile.fromJson(o.getJSONObject(name))
			}
			catch(e: Exception) { }
		}
		return result
	}

	@Synchronized
	private fun saveProfiles(profiles: Map<String, Profile>)
	{
		val o = JSONObject()
		for((name, profile) in profiles)
			o.put(name, profile.toJson())
		prefs.edit().putString(KEY_PROFILES, o.toString()).apply()
	}

	@get:Synchronized
	val currentProfileName: String get()
	{
		fixedProfile?.let { return it }
		val profiles = profiles()
		val last = prefs.getString(KEY_LAST_PROFILE, "") ?: ""
		return when
		{
			last in profiles -> last
			profiles.isNotEmpty() -> profiles.keys.first()
			else -> defaultProfileName
		}
	}

	fun currentProfile(): Profile = profiles()[currentProfileName] ?: Profile()

	@Synchronized
	fun updateCurrentProfile(change: (Profile) -> Profile)
	{
		val profiles = profiles()
		val name = currentProfileName
		profiles[name] = change(profiles[name] ?: Profile())
		saveProfiles(profiles)
	}

	/** Makes `name` current, creating it from the current profile's look (without its area) if new. */
	@Synchronized
	fun selectProfile(name: String)
	{
		val profiles = profiles()
		if(name !in profiles)
		{
			profiles[name] = currentProfile().copy(region = null, dictionaryUrl = "")
			saveProfiles(profiles)
		}
		prefs.edit().putString(KEY_LAST_PROFILE, name).apply()
	}

	@Synchronized
	fun deleteCurrentProfile()
	{
		val profiles = profiles()
		profiles.remove(currentProfileName)
		saveProfiles(profiles)
		prefs.edit().putString(KEY_LAST_PROFILE, profiles.keys.firstOrNull() ?: "").apply()
	}

	/** Adds http:// and Ollama's default port when they were left out. */
	private fun normalizeUrl(url: String): String
	{
		var u = url.trim().trimEnd('/')
		if(u.isEmpty())
			return ""
		if(!u.startsWith("http://") && !u.startsWith("https://"))
			u = "http://$u"
		val hostPart = u.substringAfter("://")
		if(!hostPart.contains(':') && !hostPart.contains('/'))
			u = "$u:11434"
		return u
	}
}
