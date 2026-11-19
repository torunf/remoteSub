// SPDX-License-Identifier: LicenseRef-AGPL-3.0-only-OpenSSL

package com.metallic.chiaki.manualconsole

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.view.Window
import android.widget.AdapterView
import android.widget.ArrayAdapter
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.Observer
import androidx.lifecycle.ViewModelProvider
import com.metallic.chiaki.R
import com.metallic.chiaki.common.RegisteredHost
import com.metallic.chiaki.common.ext.RevealActivity
import com.metallic.chiaki.common.ext.viewModelFactory
import com.metallic.chiaki.common.getDatabase
import com.metallic.chiaki.databinding.ActivityEditManualBinding

class EditManualConsoleActivity: AppCompatActivity(), RevealActivity
{
	companion object
	{
		const val EXTRA_MANUAL_HOST_ID = "manual_host_id"
	}

	private lateinit var viewModel: EditManualConsoleViewModel
	private lateinit var binding: ActivityEditManualBinding

	override val revealIntent: Intent get() = intent
	override val revealRootLayout: View get() = binding.rootLayout
	override val revealWindow: Window get() = window

	override fun onCreate(savedInstanceState: Bundle?)
	{
		super.onCreate(savedInstanceState)
		binding = ActivityEditManualBinding.inflate(layoutInflater)
		setContentView(binding.root)
		handleReveal()

		viewModel = ViewModelProvider(this, viewModelFactory {
				EditManualConsoleViewModel(getDatabase(this),
					if(intent.hasExtra(EXTRA_MANUAL_HOST_ID))
						intent.getLongExtra(EXTRA_MANUAL_HOST_ID, 0)
					else
						null)
			})
			.get(EditManualConsoleViewModel::class.java)

		viewModel.existingHost?.observe(this, Observer {
			binding.hostEditText.setText(it.host)
		})

		viewModel.selectedRegisteredHost.observe(this, Observer {
			binding.registeredHostTextView.setText(titleForRegisteredHost(it))
		})

		viewModel.registeredHosts.observe(this, Observer { hosts ->
			binding.registeredHostTextView.setAdapter(ArrayAdapter<String>(this, R.layout.dropdown_menu_popup_item,
				hosts.map { titleForRegisteredHost(it) }))
			binding.registeredHostTextView.onItemClickListener = object: AdapterView.OnItemClickListener {
				override fun onItemClick(parent: AdapterView<*>?, view: View?, position: Int, id: Long)
				{
					if(position >= hosts.size)
						return
					val host = hosts[position]
					viewModel.selectedRegisteredHost.value = host
				}
			}
		})

		// RemoteSub: the dropdown can't be driven with a TV remote (it swallows the arrows and OK),
		// so OK opens a plain list dialog instead, and Down moves on to Save.
		binding.registeredHostTextView.setOnClickListener { chooseRegisteredHost() }
		binding.registeredHostTextView.setOnKeyListener { _, keyCode, event ->
			when(keyCode)
			{
				android.view.KeyEvent.KEYCODE_DPAD_CENTER, android.view.KeyEvent.KEYCODE_ENTER ->
				{
					if(event.action == android.view.KeyEvent.ACTION_UP)
						chooseRegisteredHost()
					true
				}
				android.view.KeyEvent.KEYCODE_DPAD_DOWN ->
				{
					if(event.action == android.view.KeyEvent.ACTION_DOWN)
						binding.saveButton.requestFocus()
					true
				}
				android.view.KeyEvent.KEYCODE_DPAD_UP ->
				{
					if(event.action == android.view.KeyEvent.ACTION_DOWN)
						binding.hostEditText.requestFocus()
					true
				}
				else -> false
			}
		}

		binding.saveButton.setOnClickListener { saveHost() }
	}

	private fun chooseRegisteredHost()
	{
		// Already starts with null, "register on first connection".
		val options = viewModel.registeredHosts.value ?: listOf(null)
		com.google.android.material.dialog.MaterialAlertDialogBuilder(this)
			.setItems(options.map { titleForRegisteredHost(it) }.toTypedArray()) { _, which ->
				viewModel.selectedRegisteredHost.value = options[which]
			}
			.show()
	}

	private fun titleForRegisteredHost(registeredHost: RegisteredHost?) =
		if(registeredHost == null)
			getString(R.string.add_manual_regist_on_connect)
		else
			"${registeredHost.serverNickname ?: ""} (${registeredHost.serverMac})"

	private fun saveHost()
	{
		val host = binding.hostEditText.text.toString().trim()
		if(host.isEmpty())
		{
			binding.hostEditText.error = getString(R.string.entered_host_invalid)
			return
		}

		binding.saveButton.isEnabled = false
		viewModel.saveHost(host)
		finish()
	}
}
