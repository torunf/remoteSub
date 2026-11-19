// SPDX-License-Identifier: AGPL-3.0-only

package com.torunf.remotesub

import android.graphics.*
import android.os.Bundle
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.View
import android.widget.FrameLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.lifecycleScope
import com.metallic.chiaki.R
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * RemoteSub without a PS5: draws game-like frames with English subtitles into a SurfaceView, the
 * same kind of view the stream uses, so OCR, the dictionary, translation and the overlay can be
 * tried (and set up) before connecting to a console.
 */
class DemoActivity: AppCompatActivity()
{
	companion object
	{
		private const val PROFILE = "Demo"
		private const val SWITCH_MS = 4500L

		// Original sample lines; the first two are in the bundled demo dictionary.
		private val LINES = listOf(
			"We need to leave before the storm reaches the harbor.",
			"Nobody told me the bridge was already gone.",
			"Keep your head down and follow the river north.",
			"If they find the map, everything we did was for nothing.")
	}

	private lateinit var surfaceView: SurfaceView
	private lateinit var subtitles: SubtitleSession
	private var drawJob: Job? = null

	override fun onCreate(savedInstanceState: Bundle?)
	{
		super.onCreate(savedInstanceState)
		val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
		surfaceView = SurfaceView(this)
		root.addView(surfaceView, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
		setContentView(root)

		WindowCompat.setDecorFitsSystemWindows(window, false)
		WindowCompat.getInsetsController(window, window.decorView).apply {
			hide(WindowInsetsCompat.Type.systemBars())
			systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
		}
		window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

		val config = RemoteSubConfig(this, fixedProfile = PROFILE)
		if(config.profiles()[PROFILE] == null)
			config.updateCurrentProfile { Profile(region = Region(0.08f, 0.8f, 0.84f, 0.14f), dictionaryUrl = "asset://demo_dictionary.json") }
		subtitles = SubtitleSession.attach(this, root, surfaceView, config, allowProfiles = false)

		root.addView(TextView(this).apply {
			setText(R.string.rs_demo_hint)
			setTextColor(Color.WHITE)
			setBackgroundColor(Color.argb(150, 0, 0, 0))
			setPadding(24, 12, 24, 12)
		}, FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT).apply {
			leftMargin = 32; topMargin = 32
		})

		surfaceView.holder.addCallback(object: SurfaceHolder.Callback {
			override fun surfaceCreated(holder: SurfaceHolder) { }
			override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int)
			{
				drawJob?.cancel()
				drawJob = lifecycleScope.launch(Dispatchers.Default) {
					var i = 0
					while(isActive)
					{
						drawFrame(holder, LINES[i % LINES.size], i)
						i++
						delay(SWITCH_MS)
					}
				}
			}
			override fun surfaceDestroyed(holder: SurfaceHolder) { drawJob?.cancel() }
		})
	}

	private fun drawFrame(holder: SurfaceHolder, line: String, index: Int)
	{
		val canvas = holder.lockCanvas() ?: return
		try
		{
			val w = canvas.width.toFloat()
			val h = canvas.height.toFloat()
			// A dusky "scene" that changes a little with every line.
			val hue = (index * 47 % 360).toFloat()
			val top = Color.HSVToColor(floatArrayOf(hue, 0.45f, 0.35f))
			val bottom = Color.HSVToColor(floatArrayOf((hue + 40) % 360, 0.6f, 0.12f))
			canvas.drawPaint(Paint().apply { shader = LinearGradient(0f, 0f, 0f, h, top, bottom, Shader.TileMode.CLAMP) })
			val hills = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(110, 0, 0, 0) }
			canvas.drawOval(-0.2f * w, 0.55f * h, 0.7f * w, 1.3f * h, hills)
			canvas.drawOval(0.4f * w, 0.5f * h, 1.3f * w, 1.25f * h, hills)

			// The subtitle, white with a dark outline like most games.
			val size = h * 0.042f
			val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
				textSize = size; color = Color.WHITE; textAlign = Paint.Align.CENTER
				typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
			}
			val stroke = Paint(fill).apply { style = Paint.Style.STROKE; strokeWidth = size * 0.12f; color = Color.BLACK }
			val y = h * 0.885f
			canvas.drawText(line, w / 2, y, stroke)
			canvas.drawText(line, w / 2, y, fill)
		}
		finally
		{
			holder.unlockCanvasAndPost(canvas)
		}
	}

	override fun dispatchKeyEvent(event: KeyEvent) = subtitles.dispatchKeyEvent(event) || super.dispatchKeyEvent(event)
	override fun onGenericMotionEvent(event: MotionEvent) = subtitles.onGenericMotionEvent(event) || super.onGenericMotionEvent(event)
}
