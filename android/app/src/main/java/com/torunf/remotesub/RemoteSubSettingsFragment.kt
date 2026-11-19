// SPDX-License-Identifier: AGPL-3.0-only

package com.torunf.remotesub

import android.content.Intent
import android.content.res.Resources
import android.os.Bundle
import android.text.InputType
import androidx.lifecycle.lifecycleScope
import androidx.preference.EditTextPreference
import androidx.preference.ListPreference
import androidx.preference.Preference
import androidx.preference.PreferenceFragmentCompat
import com.google.mlkit.common.model.DownloadConditions
import com.metallic.chiaki.R
import com.metallic.chiaki.settings.TitleFragment
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withContext
import java.util.Locale

class RemoteSubSettingsFragment: PreferenceFragmentCompat(), TitleFragment
{
	override fun onCreatePreferences(savedInstanceState: Bundle?, rootKey: String?)
	{
		preferenceManager.sharedPreferencesName = RemoteSubConfig.PREFS_NAME
		setPreferencesFromResource(R.xml.remotesub_preferences, rootKey)
		val config = RemoteSubConfig(requireContext())

		findPreference<Preference>("demo")?.setOnPreferenceClickListener {
			startActivity(Intent(requireContext(), DemoActivity::class.java))
			true
		}

		findPreference<EditTextPreference>(RemoteSubConfig.KEY_OLLAMA_URL)?.apply {
			setOnBindEditTextListener { it.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI }
			summaryProvider = Preference.SummaryProvider<EditTextPreference> { p ->
				p.text?.takeIf { it.isNotBlank() } ?: getString(R.string.rs_ollama_url_summary_empty)
			}
		}
		findPreference<EditTextPreference>(RemoteSubConfig.KEY_MODEL)?.summaryProvider =
			Preference.SummaryProvider<EditTextPreference> { p ->
				p.text?.takeIf { it.isNotBlank() } ?: getString(R.string.rs_model_summary_auto)
			}

		findPreference<Preference>("ollama_test")?.setOnPreferenceClickListener { pref ->
			val url = config.ollamaUrl
			if(url.isEmpty())
			{
				pref.summary = getString(R.string.rs_ollama_url_summary_empty)
				return@setOnPreferenceClickListener true
			}
			pref.summary = getString(R.string.rs_ollama_testing)
			lifecycleScope.launch {
				val result = withContext(Dispatchers.IO) { runCatching { Translator.ollamaModels(url) } }
				pref.summary = result.fold(
					onSuccess = { models ->
						val chosen = config.model.ifEmpty { models.firstOrNull() ?: "" }
						if(models.isEmpty()) getString(R.string.rs_ollama_no_model) else getString(R.string.rs_ollama_ok, chosen)
					},
					onFailure = { getString(R.string.rs_ollama_failed, it.message ?: it.toString()) })
			}
			true
		}

		fun languageList(key: String, codes: List<String>)
		{
			findPreference<ListPreference>(key)?.apply {
				entryValues = codes.toTypedArray()
				entries = codes.map { Locale(it).getDisplayLanguage(Locale.getDefault()).replaceFirstChar { c -> c.titlecase() } }.toTypedArray()
			}
		}
		languageList(RemoteSubConfig.KEY_SOURCE, RemoteSubConfig.SOURCE_LANGUAGES)
		languageList(RemoteSubConfig.KEY_TARGET, RemoteSubConfig.TARGET_LANGUAGES)

		findPreference<Preference>("on_device_model")?.setOnPreferenceClickListener { pref ->
			pref.summary = getString(R.string.rs_ondevice_downloading)
			lifecycleScope.launch {
				val client = Translator.onDeviceTranslator(config.source, config.target)
				pref.summary = try
				{
					client.downloadModelIfNeeded(DownloadConditions.Builder().build()).await()
					getString(R.string.rs_ondevice_ready)
				}
				catch(e: Exception)
				{
					getString(R.string.rs_ondevice_failed, e.message ?: e.toString())
				}
				finally
				{
					client.close()
				}
			}
			true
		}
	}

	override fun getTitle(resources: Resources): String = resources.getString(R.string.rs_settings_title)
}
