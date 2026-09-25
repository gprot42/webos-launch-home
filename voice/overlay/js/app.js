(function () {
  'use strict';

  // Launch Home's own voice daemon (voice/daemon), never another project's.
  var EVENTS_WS_URI = 'ws://127.0.0.1:8678';
  window.__dbg = window.__dbg || [];
  function dbg(m) {
    // Debug tracing disabled in production. Left as a no-op so the many
    // dbg() call sites don't need removing.
    return;
    /* eslint-disable no-unreachable */
    try {
      window.__dbg.push(Date.now() + ' ' + m);
      if (window.__dbg.length > 300) {
        window.__dbg.shift();
      }
    } catch (e) {
      // ignore
    }
    try {
      console.log('[voice-dbg] ' + m);
    } catch (e) {
      // ignore
    }
  }
  var currentMode = null;
  var voiceBridge = null;
  var answerBuffer = '';
  // When TTS is on, hold answer text until the first audio chunk plays so the
  // user does not read a full reply in silence (text→speech gap).
  var holdAnswerForTts = false;
  var answerRevealed = '';
  var holdAnswerTimer = null;
  // Only a short safety net if TTS is still arming; answer tokens stream live
  // so the user is never stuck on Thinking… waiting for speech synthesis.
  var HOLD_ANSWER_SAFETY_MS = 900;
  // Pending RPCs sent over the WS, keyed by request id. The sandboxed app has
  // no Luna access to the daemon, so requests share the voice-event socket.
  var wsPending = {};
  var wsSeq = 0;
  var WS_RPC_TIMEOUT_MS = 8000;
  var bridgeOpening = null;
  var wsReconnectAttempts = 0;
  var wsReconnectTimer = null;
  var WS_RECONNECT_MAX = 8;
  var nextAudioSeq = 1;
  var pendingAudioBySeq = {};

  var overlayEl = document.getElementById('overlay');
  var statusEl = document.getElementById('status');
  var transcriptText = document.getElementById('transcript-text');
  var cursor = document.getElementById('cursor');
  var answerPanel = document.getElementById('answer-panel');
  var answerLabel = document.getElementById('answer-label');
  var answerText = document.getElementById('answer-text');
  var listeningOrb = document.getElementById('listening-orb');
  // Video listening asset removed — CSS pulse only (no forest stills).
  var listeningOrbVideo = null;
  var thinkingIndicator = document.getElementById('thinking-indicator');
  var answerAudioEl = null;
  var audioQueue = [];
  var audioPlaying = false;
  var pendingClose = false;
  // Spoken-answer completion tracking. The daemon streams TTS as ordered
  // chunks and then sends `ttsComplete` with the total count. We only tell the
  // daemon the answer has finished playing (`playbackEnded`) once every chunk
  // has actually been played, so the window stays open until speech is done.
  var ttsExpected = null;
  var ttsPlayed = 0;
  var playbackReported = false;

  function getLaunchParams() {
    try {
      if (window.PalmSystem && window.PalmSystem.launchParams) {
        var raw = window.PalmSystem.launchParams;
        if (typeof raw === 'string' && raw.length) {
          return JSON.parse(raw);
        }
        if (typeof raw === 'object' && raw) {
          return raw;
        }
      }
    } catch (e) {
      // ignore
    }
    return {};
  }

  function isVoiceLaunch(params) {
    if (!params) {
      return false;
    }
    return params.source === 'launchhome' || params.mode === 'voice';
  }

  function scheduleWsReconnect() {
    if (pendingClose || currentMode !== 'voice') {
      return;
    }
    if (wsReconnectAttempts >= WS_RECONNECT_MAX) {
      try {
        setVoiceStatus('Reconnecting failed');
      } catch (e) {
        // ignore
      }
      return;
    }
    if (wsReconnectTimer) {
      return;
    }
    var delay = Math.min(8000, 300 * Math.pow(1.7, wsReconnectAttempts));
    wsReconnectAttempts += 1;
    try {
      setVoiceStatus('Reconnecting…');
    } catch (e2) {
      // ignore
    }
    wsReconnectTimer = setTimeout(function () {
      wsReconnectTimer = null;
      openBridge().catch(function () {
        scheduleWsReconnect();
      });
    }, delay);
  }

  function openBridge() {
    if (voiceBridge &&
        (voiceBridge.readyState === 0 || voiceBridge.readyState === 1)) {
      if (voiceBridge.readyState === 1) {
        return Promise.resolve(voiceBridge);
      }
      // Still connecting — reuse the in-flight open promise if present.
      if (bridgeOpening) {
        return bridgeOpening;
      }
    }
    // Close any stale socket before opening a new primary connection.
    if (voiceBridge) {
      try {
        voiceBridge.onclose = null;
        voiceBridge.close();
      } catch (eClose) {
        // ignore
      }
      voiceBridge = null;
    }
    bridgeOpening = new Promise(function (resolve, reject) {
      var ws;
      try {
        ws = new WebSocket(EVENTS_WS_URI);
      } catch (e) {
        bridgeOpening = null;
        reject(new Error('Cannot reach the voice service'));
        return;
      }
      voiceBridge = ws;
      ws.onopen = function () {
        bridgeOpening = null;
        wsReconnectAttempts = 0;
        // Tell the daemon this is the voice card (not Launch Home), so it
        // counts as the connected overlay and gets the spoken audio.
        try {
          ws.send(JSON.stringify({type: 'hello', id: 'hello', params: {role: 'overlay'}}));
        } catch (e) {
          // ignore
        }
        resolve(ws);
      };
      ws.onmessage = function (ev) {
        var res;
        try {
          res = JSON.parse(ev.data);
        } catch (e) {
          return;
        }
        if (res.event === 'configResult') {
          var pending = wsPending[res.id];
          if (!pending) {
            return;
          }
          delete wsPending[res.id];
          clearTimeout(pending.timer);
          if (res.ok) {
            pending.resolve(res.result || {});
          } else {
            pending.reject(new Error(res.error || 'Request failed'));
          }
          return;
        }
        if (res.event) {
          handleVoiceEvent(res.event, res.payload);
        }
      };
      ws.onclose = function () {
        if (voiceBridge === ws) {
          voiceBridge = null;
        }
        if (bridgeOpening) {
          bridgeOpening = null;
          reject(new Error('Connection closed'));
        }
        // Fail any in-flight RPCs so callers don't hang.
        Object.keys(wsPending).forEach(function (id) {
          var p = wsPending[id];
          delete wsPending[id];
          clearTimeout(p.timer);
          p.reject(new Error('Connection closed'));
        });
        // Mid-answer disconnect: reconnect with backoff (daemon keeps one primary).
        if (currentMode === 'voice' && !pendingClose) {
          scheduleWsReconnect();
        }
      };
      ws.onerror = function () {
        console.error('[voice] ws error');
      };
    });
    return bridgeOpening;
  }

  function lunaCall(method, params) {
    return openBridge().then(function (ws) {
      return new Promise(function (resolve, reject) {
        var id = 'r' + (++wsSeq);
        var timer = setTimeout(function () {
          if (wsPending[id]) {
            delete wsPending[id];
            reject(new Error('Request timed out'));
          }
        }, WS_RPC_TIMEOUT_MS);
        wsPending[id] = { resolve: resolve, reject: reject, timer: timer };
        try {
          ws.send(JSON.stringify({ type: method, params: params || {}, id: id }));
        } catch (e) {
          delete wsPending[id];
          clearTimeout(timer);
          reject(e);
        }
      });
    });
  }

  function showMode(mode) {
    currentMode = mode;
    if (mode === 'voice') {
      document.body.classList.add('voice-mode');
      if (overlayEl) {
        overlayEl.classList.remove('hidden');
      }
      ensureVoiceSubscription();
      return;
    }

    // Settings live in Launch Home's AI Voice tab: anything but a voice
    // launch just closes the card.
    closeApp();
  }

  /** Force the listening/answer UI on screen (never leave a blank card). */
  function ensureVoiceUi() {
    // No-op when already on the voice stage — repeated showMode/class toggles
    // on webOS reflow the whole card (user-visible double redraw after answer).
    if (
      currentMode === 'voice' &&
      overlayEl &&
      !overlayEl.classList.contains('hidden') &&
      document.body.classList.contains('voice-mode')
    ) {
      return;
    }
    showMode('voice');
    if (overlayEl) {
      overlayEl.classList.remove('hidden');
    }
    document.body.classList.add('voice-mode');
  }

  function answerIsVisible() {
    var shown = '';
    try {
      shown = (answerText && answerText.textContent) || '';
    } catch (e) {
      shown = '';
    }
    return !!(
      (answerRevealed || '').trim() ||
      (answerBuffer || '').trim() ||
      String(shown).trim()
    );
  }

  function closeApp() {
    if (typeof window.close === 'function') {
      window.close();
    }
  }

  function stageReady() {
    try {
      if (window.PalmSystem) {
        if (typeof window.PalmSystem.stageReady === 'function') {
          window.PalmSystem.stageReady();
        }
        if (typeof window.PalmSystem.activate === 'function') {
          window.PalmSystem.activate();
        }
      }
    } catch (e) {
      // ignore
    }
  }

  // Bumped on every sessionStarted so a late sessionEnded from the previous
  // turn cannot close the card after a new question has begun.
  var sessionUiGen = 0;
  var closeGen = 0;

  function finishSessionClose() {
    dbg('finishSessionClose');
    // Abort if a newer voice turn already started (stale sessionEnded).
    if (closeGen !== sessionUiGen) {
      dbg('finishSessionClose skipped stale gen');
      pendingClose = false;
      return;
    }
    pendingClose = false;
    stopAnswerAudio();
    setListeningOrb(false);
    try {
      if (overlayEl) {
        overlayEl.classList.add('hidden');
      }
    } catch (e) {
      // ignore
    }
    // Soft hide only — keep WebAppMgr process alive so the next KEY_VOICE
    // is a warm relaunch (~instant) instead of a cold SAM start (~1–2s).
    // Hard close (window.close) only when the daemon force-kills for Netflix.
    try {
      document.body.classList.remove('voice-mode');
    } catch (e2) {
      // ignore
    }
  }

  function maybeReportPlaybackEnded() {
    // Fire once, only after every streamed chunk has finished playing. A
    // momentarily empty queue mid-answer (synthesis still catching up) must NOT
    // trigger this, so we gate on the daemon-provided total (ttsExpected).
    if (playbackReported) {
      return;
    }
    // ttsExpected stays null until ttsComplete. If complete said 0 chunks, or
    // we have drained the queue after complete, report done.
    if (ttsExpected === null) {
      return;
    }
    if (audioPlaying || audioQueue.length) {
      return;
    }
    if (ttsExpected > 0 && ttsPlayed < ttsExpected) {
      return;
    }
    playbackReported = true;
    // Fill any remainder the progressive chunk reveals may have missed.
    if (answerBuffer && answerBuffer.length > (answerRevealed || '').length) {
      answerRevealed = answerBuffer;
      paintAnswerVisible();
    }
    dbg('playbackEnded report played=' + ttsPlayed + '/' + ttsExpected);
    try {
      lunaCall('playbackEnded', {}).then(function () {
        dbg('playbackEnded ack');
      }).catch(function (e) {
        dbg('playbackEnded send failed ' + (e && e.message));
      });
    } catch (e) {
      // best-effort: the daemon still has a duration-based close timer
    }
    // If the daemon already asked us to close (sessionEnded while audio was
    // still draining), finish the hide/close now that speech is done.
    if (pendingClose) {
      finishSessionClose();
    }
  }

  function clearHoldAnswerTimer() {
    if (holdAnswerTimer) {
      clearTimeout(holdAnswerTimer);
      holdAnswerTimer = null;
    }
  }

  function armHoldAnswerForTts() {
    holdAnswerForTts = true;
    clearHoldAnswerTimer();
    // If synthesis fails or audio never arrives, still show the text.
    holdAnswerTimer = setTimeout(function () {
      holdAnswerTimer = null;
      revealAnswerFull();
    }, HOLD_ANSWER_SAFETY_MS);
  }

  function paintAnswerVisible() {
    var next = answerRevealed || '';
    // Skip no-op paints — rewriting the same answer + toggling classes made
    // the card flash after the response was already on screen.
    if (answerText && answerText.textContent === next && next) {
      setListeningOrb(false, { thinking: true });
      setThinking(!audioPlaying);
      return;
    }
    answerText.textContent = next;
    if (next) {
      // Keep the pulse logo while TTS catches up to the on-screen text.
      setListeningOrb(false, { thinking: true });
      setThinking(!audioPlaying);
      answerLabel.classList.remove('hidden');
      answerPanel.scrollTop = answerPanel.scrollHeight;
      if (
        statusEl &&
        (statusEl.textContent || '').trim() &&
        statusEl.textContent.indexOf('Preparing') < 0
      ) {
        statusEl.textContent = '';
      }
    }
  }

  function revealAnswerFull() {
    holdAnswerForTts = false;
    clearHoldAnswerTimer();
    answerRevealed = answerBuffer || answerRevealed;
    paintAnswerVisible();
  }

  function revealAnswerChunk(chunkText) {
    holdAnswerForTts = false;
    clearHoldAnswerTimer();
    // Live stream already painted the full answer — do not re-layout on each
    // TTS chunk (that was a second full redraw while audio started).
    if (
      answerBuffer &&
      answerRevealed === answerBuffer &&
      answerText &&
      answerText.textContent === answerBuffer
    ) {
      setThinking(false);
      setListeningOrb(false);
      return;
    }
    var t = (chunkText || '').trim();
    if (t) {
      if (!answerRevealed) {
        answerRevealed = t;
      } else {
        // Avoid duplicating if a catch-up final already filled the buffer.
        if (answerRevealed.indexOf(t) === -1) {
          var needsSpace = !/\s$/.test(answerRevealed) && !/^[.,!?;:]/.test(t);
          answerRevealed += (needsSpace ? ' ' : '') + t;
        }
      }
    } else if (!answerRevealed) {
      answerRevealed = answerBuffer;
    }
    paintAnswerVisible();
  }

  function resetVoicePanels() {
    stopAnswerAudio();
    pendingClose = false;
    ttsExpected = null;
    ttsPlayed = 0;
    playbackReported = false;
    nextAudioSeq = 1;
    pendingAudioBySeq = {};
    wsReconnectAttempts = 0;
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = null;
    }
    clearHoldAnswerTimer();
    holdAnswerForTts = false;
    answerRevealed = '';
    statusEl.textContent = 'Listening…';
    transcriptText.textContent = '';
    answerBuffer = '';
    answerText.textContent = '';
    answerLabel.textContent = 'Grok:';
    answerLabel.classList.remove('source-internet', 'source-grok');
    answerLabel.classList.add('hidden');
    answerPanel.classList.remove('error', 'thinking');
    cursor.classList.remove('hidden');
    setThinking(false);
    setListeningOrb(true);
  }

  function setVoiceStatus(text) {
    var next = text || '';
    var prev = statusEl ? (statusEl.textContent || '') : '';
    if (statusEl && prev === next) {
      return;
    }
    if (statusEl) {
      statusEl.textContent = next;
    }
    var t = next.toLowerCase();
    if (t.indexOf('listen') === 0) {
      setListeningOrb(true);
      setThinking(false);
      return;
    }
    // Keep the pulse logo visible while we wait for / prepare speech.
    // Hiding it on "Preparing voice…" felt like the UI went blank.
    var keepPulse = /prepar|writing|speak|think|transcrib|search|ask|got|looking|check|double|generat|wait|refin|gemini|asking/.test(t);
    if (answerIsVisible()) {
      // Answer text is up — dimmed pulse until audio starts (not full hide).
      if (!audioPlaying) {
        setListeningOrb(false, { thinking: true });
        setThinking(true);
      } else {
        setListeningOrb(false, { thinking: true });
        setThinking(false);
      }
      return;
    }
    if (keepPulse || t === '') {
      setListeningOrb(false, { thinking: true });
      setThinking(true);
    } else {
      setListeningOrb(false);
      setThinking(false);
    }
  }

  function setListeningOrb(on, opts) {
    if (!listeningOrb) {
      return;
    }
    opts = opts || {};
    var thinking = !!opts.thinking;
    var wantVisible = !!(on || thinking);
    var isVisible = listeningOrb.classList.contains('visible');
    var isThinking = listeningOrb.classList.contains('thinking');
    if (wantVisible === isVisible && thinking === isThinking) {
      return;
    }
    listeningOrb.classList.add('no-video');
    listeningOrb.classList.remove('has-video');
    if (wantVisible) {
      listeningOrb.classList.add('visible');
      listeningOrb.classList.toggle('thinking', thinking);
    } else {
      listeningOrb.classList.remove('visible', 'thinking', 'has-video');
    }
  }

  function setThinking(on) {
    var dotsOn = thinkingIndicator && !thinkingIndicator.classList.contains('hidden');
    var panelOn = answerPanel && answerPanel.classList.contains('thinking');
    if (on === !!dotsOn && on === !!panelOn) {
      return;
    }
    if (thinkingIndicator) {
      if (on) {
        thinkingIndicator.classList.remove('hidden');
      } else {
        thinkingIndicator.classList.add('hidden');
      }
    }
    if (answerPanel) {
      if (on) {
        answerPanel.classList.add('thinking');
      } else {
        answerPanel.classList.remove('thinking');
      }
    }
  }

  function handleVoiceEvent(name, payload) {
    payload = payload || {};
    switch (name) {
      case 'sessionStarted':
        dbg('sessionStarted');
        // Invalidate any pending close from the previous turn.
        sessionUiGen += 1;
        closeGen = sessionUiGen;
        pendingClose = false;
        // Always force the voice stage — a residual blank card must
        // not stay on top of Listening. Do not hide/reopen the whole app.
        ensureVoiceUi();
        if (overlayEl) {
          overlayEl.classList.remove('hidden');
        }
        // Fresh press only. Catch-up uses sessionCatchup so mid-utterance
        // reconnects do not wipe the question the user already sees.
        if (!(payload && payload.catchup)) {
          resetVoicePanels();
        }
        break;
      case 'sessionCatchup':
        // Late WS attach (cold launch / focus). Restore state without reset.
        dbg('sessionCatchup');
        ensureVoiceUi();
        if (payload.transcript) {
          var catchT = String(payload.transcript).trim();
          var curT = (transcriptText.textContent || '').trim();
          if (!curT || catchT.length >= curT.length) {
            transcriptText.textContent = catchT;
            cursor.classList.add('hidden');
          }
        }
        if (payload.source) {
          applyAnswerSource(payload.source);
        }
        if (payload.answer) {
          answerBuffer = String(payload.answer);
          // If audio is not already holding the reveal, show restored answer.
          if (!holdAnswerForTts) {
            answerRevealed = answerBuffer;
            answerText.textContent = answerBuffer;
            answerLabel.classList.remove('hidden');
            setThinking(false);
            setListeningOrb(false);
            setVoiceStatus('');
          }
        } else if (payload.status) {
          setVoiceStatus(String(payload.status));
        } else if (payload.capture_active) {
          setVoiceStatus('Listening…');
          setListeningOrb(true);
        } else if ((transcriptText.textContent || '').trim()) {
          setVoiceStatus('Looking up your answer…');
        }
        break;
      case 'listeningEnded':
        // Mic stopped — stay on the same dark stage with a dim orb + dots
        // (never tear down to a blank/black frame).
        ensureVoiceUi();
        var endedText = (transcriptText.textContent || '').trim() ||
          String((payload && payload.transcript) || '').trim();
        if (endedText) {
          transcriptText.textContent = endedText;
          cursor.classList.add('hidden');
        }
        var hasAudio = !!(payload && payload.has_audio);
        // Clearer than generic Thinking… — daemon will refine with Asking Grok…
        setVoiceStatus(
          endedText ? 'Got your question…' : (hasAudio ? 'Finishing speech…' : 'Transcribing…')
        );
        setListeningOrb(false, { thinking: true });
        setThinking(true);
        break;
      case 'status':
        // Daemon stage line: Asking Grok… / Searching the web… / Writing answer…
        if (payload && payload.text) {
          setVoiceStatus(String(payload.text));
        }
        break;
      case 'transcriptPartial':
        // Live STT: prefer growing text. Reject short regressions so a bad
        // mid-phrase guess ("come from") cannot wipe a better longer hypothesis.
        ensureVoiceUi();
        if (payload.text !== undefined && payload.text !== null) {
          var nextP = String(payload.text).trim();
          var curP = (transcriptText.textContent || '').trim();
          var isFinalP = !!payload.is_final;
          // Ellipsis = daemon is re-transcribing; clear the wrong hypothesis.
          var recheckClear = nextP === '…' || nextP === '...' || nextP === '…';
          var acceptP = recheckClear || !curP ||
            nextP.length >= curP.length ||
            nextP.length >= Math.max(8, Math.floor(curP.length * 0.7)) ||
            (isFinalP && nextP.length >= Math.max(6, Math.floor(curP.length * 0.55)));
          if (acceptP) {
            transcriptText.textContent = recheckClear ? '…' : nextP;
            cursor.classList.add('hidden');
            // Keep Listening status only while mic is still active; finalize
            // may set "Double-checking speech…" via status events.
            if (!recheckClear && !isFinalP) {
              var st = (statusEl && statusEl.textContent) || '';
              if (!st || st.indexOf('Listening') === 0) {
                setVoiceStatus('Listening…');
              }
            }
          }
        }
        break;
      case 'transcriptFinal':
        // Prefer the longer of final STT vs on-screen partials. A short final
        // ("UK today.") must not erase a longer live question.
        ensureVoiceUi();
        var finalT = String(payload.text || '').trim();
        var shownT = (transcriptText.textContent || '').trim();
        if (finalT && shownT && finalT.length < Math.max(10, Math.floor(shownT.length * 0.6))) {
          dbg('transcriptFinal shorter — keeping shown');
          // keep shownT
        } else if (finalT) {
          transcriptText.textContent = finalT;
        } else if (!shownT) {
          transcriptText.textContent = '';
        }
        cursor.classList.add('hidden');
        setVoiceStatus('Thinking…');
        // New question: clear previous answer, keep the question text.
        answerText.textContent = '';
        answerBuffer = '';
        answerRevealed = '';
        break;
      case 'ttsWillSpeak':
        // TTS still runs in parallel, but we no longer blank the answer while
        // waiting for the first audio chunk — that made "Thinking…" last many
        // seconds after the transcript was already on screen.
        if (!payload || payload.enabled !== false) {
          // Soft arm only if nothing has streamed yet (race: audio before text).
          if (!(answerBuffer || '').trim() && !(answerRevealed || '').trim()) {
            armHoldAnswerForTts();
          }
        } else {
          holdAnswerForTts = false;
          clearHoldAnswerTimer();
        }
        break;
      case 'answerPartial':
        answerBuffer += payload.text || '';
        // Always paint streaming tokens — first visible answer ASAP.
        holdAnswerForTts = false;
        clearHoldAnswerTimer();
        // Keep the pulse logo (dimmed) while text streams and TTS catches up.
        setListeningOrb(false, { thinking: true });
        setThinking(!audioPlaying);
        if (answerLabel.classList.contains('hidden')) {
          answerLabel.classList.remove('hidden');
        }
        answerRevealed = answerBuffer;
        if (answerText.textContent !== answerBuffer) {
          answerText.textContent = answerBuffer;
          answerPanel.scrollTop = answerPanel.scrollHeight;
        }
        // Status only on first paint; leave it alone while tokens stream.
        if ((answerBuffer || '').length > 0 && (answerBuffer || '').length < 20) {
          if (statusEl && statusEl.textContent !== 'Writing answer…') {
            statusEl.textContent = 'Writing answer…';
          }
        } else if ((answerBuffer || '').length >= 20 && statusEl && statusEl.textContent) {
          // Prefer a quieter status once text is flowing; keep pulse via above.
          if (!audioPlaying && statusEl.textContent.indexOf('Preparing') < 0) {
            statusEl.textContent = '';
          }
        }
        break;
      case 'answerFinal':
        answerBuffer = payload.text || answerBuffer;
        holdAnswerForTts = false;
        clearHoldAnswerTimer();
        // Keep pulse until speech starts.
        setListeningOrb(false, { thinking: true });
        setThinking(!audioPlaying);
        if (answerLabel.classList.contains('hidden')) {
          answerLabel.classList.remove('hidden');
        }
        answerRevealed = answerBuffer;
        if (answerText.textContent !== answerBuffer) {
          answerText.textContent = answerBuffer;
        }
        if (statusEl && statusEl.textContent && statusEl.textContent.indexOf('Preparing') < 0) {
          statusEl.textContent = '';
        }
        break;
      case 'answerAudio':
        // Audio is live — keep a soft pulse, drop the "thinking" dots only.
        setListeningOrb(false, { thinking: true });
        setThinking(false);
        if (statusEl) {
          statusEl.textContent = '';
        }
        playAnswerAudio(payload);
        break;
      case 'answerSource':
        applyAnswerSource(payload);
        break;
      case 'ttsComplete':
        // Full answer synthesised: now we know how many chunks make a complete
        // spoken answer. If playback already drained (short answer), report at
        // once; otherwise the queue-drain path will report when it catches up.
        ttsExpected = typeof payload.count === 'number' ? payload.count : 0;
        dbg('ttsComplete expected=' + ttsExpected + ' played=' + ttsPlayed);
        // No audio chunks (TTS failed / empty) — show the text anyway.
        if (ttsExpected === 0) {
          revealAnswerFull();
        }
        maybeReportPlaybackEnded();
        break;
      case 'error':
        // Ensure the voice overlay is visible.
        showMode('voice');
        overlayEl.classList.remove('hidden');
        holdAnswerForTts = false;
        clearHoldAnswerTimer();
        setThinking(false);
        setListeningOrb(false);
        answerPanel.classList.add('error');
        answerPanel.classList.remove('thinking');
        answerLabel.classList.remove('hidden');
        answerText.textContent = payload.message || 'Something went wrong';
        setVoiceStatus('');
        cursor.classList.add('hidden');
        break;
      case 'sessionEnded':
        dbg('sessionEnded playing=' + audioPlaying + ' qlen=' + audioQueue.length);
        // Capture gen now — if sessionStarted bumps sessionUiGen before we
        // actually close, finishSessionClose will no-op (no mid-question flash).
        closeGen = sessionUiGen;
        // Always mark pendingClose first so WS onclose does not show
        // "Reconnecting…" during silent app launch (Terminal/Prime).
        pendingClose = true;
        if (wsReconnectTimer) {
          clearTimeout(wsReconnectTimer);
          wsReconnectTimer = null;
        }
        setVoiceStatus('');
        // The daemon ends the session as soon as TTS *synthesis* finishes, but
        // the audio still needs several seconds to *play*. If we stopped and
        // closed now we would cut the spoken answer off (the long-standing "no
        // audio" bug). So if audio is still in flight, defer the hide/close
        // until playback drains; otherwise close after a short grace so the
        // user can still read the answer (no instant black flash).
        if (audioPlaying || audioQueue.length) {
          // pendingClose already true — wait for playbackEnded.
        } else {
          // Soft delay: avoids slam-close then relaunch if a new press is near.
          setTimeout(function () {
            if (closeGen === sessionUiGen) {
              finishSessionClose();
            }
          }, 400);
        }
        break;
    }
  }

  function applyAnswerSource(payload) {
    var source = (payload && payload.source) || 'grok';
    var cites = (payload && payload.citations) || [];
    answerLabel.classList.remove('hidden');
    answerLabel.classList.remove(
      'source-internet', 'source-grok', 'source-tv', 'source-gemini'
    );
    if (source === 'internet') {
      var n = cites.length;
      answerLabel.textContent = n
        ? 'Internet (' + n + ' source' + (n === 1 ? '' : 's') + '):'
        : 'Internet:';
      answerLabel.classList.add('source-internet');
    } else if (source === 'tv') {
      answerLabel.textContent = 'TV:';
      answerLabel.classList.add('source-tv');
    } else if (source === 'gemini') {
      answerLabel.textContent = 'Gemini:';
      answerLabel.classList.add('source-gemini');
    } else {
      answerLabel.textContent = 'Grok:';
      answerLabel.classList.add('source-grok');
    }
  }

  function enqueueAudioItem(item) {
    audioQueue.push(item);
    if (!audioPlaying) {
      playNextAudio();
    }
  }

  function flushPendingAudioSeq() {
    // Play in seq order when chunks arrive out of order.
    while (pendingAudioBySeq[nextAudioSeq]) {
      var ready = pendingAudioBySeq[nextAudioSeq];
      delete pendingAudioBySeq[nextAudioSeq];
      nextAudioSeq += 1;
      enqueueAudioItem(ready);
    }
  }

  function playAnswerAudio(payload) {
    var b64 = payload && payload.audio;
    if (!b64) {
      dbg('playAnswerAudio EMPTY payload');
      return;
    }
    dbg('playAnswerAudio recv b64len=' + b64.length + ' audioPlaying=' + audioPlaying + ' qlen=' + audioQueue.length);
    // Do NOT pre-create/load an <audio> element here. On this webOS WebKit,
    // prefetch+load() then later play() often resumes mid-clip so the first
    // words of the answer are never heard. Fresh element at play time only.
    var item = {
      b64: b64,
      mime: (payload && payload.mime) || 'audio/mpeg',
      // Always play at native rate. This webOS WebKit outputs SILENCE when
      // playbackRate != 1.0 (time-stretch resampler bug), so faster speech
      // must come from the TTS API itself, never from playbackRate here.
      speed: 1.0,
      // Spoken wording — revealed only when audio is actually playing.
      text: (payload && payload.text) || '',
      seq: typeof payload.seq === 'number' ? payload.seq : 0,
      el: null
    };
    if (item.seq > 0) {
      if (item.seq < nextAudioSeq) {
        // Late duplicate / already passed — drop.
        return;
      }
      if (item.seq > nextAudioSeq) {
        pendingAudioBySeq[item.seq] = item;
        // Never skip seq 1 (that drops the start of the spoken answer).
        // For later holes only, wait longer before jumping.
        if (nextAudioSeq <= 1) {
          return;
        }
        setTimeout(function () {
          if (nextAudioSeq <= 1) {
            return;
          }
          if (nextAudioSeq < item.seq && !pendingAudioBySeq[nextAudioSeq]) {
            dbg('seq hole skip next=' + nextAudioSeq + ' to=' + item.seq);
            nextAudioSeq = item.seq;
            flushPendingAudioSeq();
          }
        }, 4000);
        return;
      }
      // item.seq === nextAudioSeq
      nextAudioSeq += 1;
      enqueueAudioItem(item);
      flushPendingAudioSeq();
      return;
    }
    enqueueAudioItem(item);
  }

  function b64ToBlobUrl(b64, mime) {
    // webOS WebKit stalls indefinitely on large `data:` audio URIs
    // (readyState stuck at HAVE_NOTHING). A Blob object URL loads through the
    // media pipeline and plays reliably, so decode base64 -> bytes -> Blob.
    var binary = atob(b64);
    var len = binary.length;
    var bytes = new Uint8Array(len);
    for (var i = 0; i < len; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    var blob = new Blob([bytes], { type: mime || 'audio/mpeg' });
    return URL.createObjectURL(blob);
  }

  function playNextAudio() {
    if (!audioQueue.length) {
      dbg(
        'playNext queue empty played=' +
          ttsPlayed +
          ' expected=' +
          ttsExpected
      );
      audioPlaying = false;
      answerAudioEl = null;
      // Synthesis still in flight: do NOT report ended or close — later
      // answerAudio chunks will re-enter via enqueueAudioItem → playNext.
      if (ttsExpected === null || (ttsExpected > 0 && ttsPlayed < ttsExpected)) {
        dbg('playNext waiting for more TTS chunks');
        return;
      }
      if (pendingClose) {
        finishSessionClose();
      } else {
        maybeReportPlaybackEnded();
      }
      return;
    }
    audioPlaying = true;
    var item = audioQueue.shift();
    dbg('playNext start mime=' + item.mime + ' speed=' + item.speed + ' remaining=' + audioQueue.length);
    // Do NOT paint answer text here — data: URI decode can lag on webOS and
    // that was the "poem on screen, silence" gap. Reveal on 'playing' only.
    // Always a brand-new element so playback starts at t=0 (prefetched els
    // on this TV often resumed mid-clip → missing opening words).
    var el = null;
    var blobUrl = null;
    try {
      el = new Audio(
        'data:' + (item.mime || 'audio/mpeg') + ';base64,' + item.b64
      );
      el.preload = 'auto';
    } catch (e) {
      dbg('tts init failed ' + (e && e.message));
      console.error('[voice] tts init failed', e);
      answerAudioEl = null;
      playNextAudio();
      return;
    }
    answerAudioEl = el;
    var advanced = false;
    var revealed = false;
    var watchdog = null;
    var rewoundOnce = false;
    // webOS can leave media muted / volume 0 after LG voice UI steals focus.
    try {
      el.muted = false;
      el.volume = 1.0;
    } catch (eVol) {
      // ignore
    }
    function revealNow() {
      if (revealed) {
        return;
      }
      revealed = true;
      if (item.text) {
        revealAnswerChunk(item.text);
      } else if (holdAnswerForTts || !answerRevealed) {
        revealAnswerFull();
      }
    }
    function advance() {
      if (advanced) {
        return;
      }
      advanced = true;
      ttsPlayed += 1;
      if (watchdog) {
        clearTimeout(watchdog);
        watchdog = null;
      }
      if (blobUrl) {
        try { URL.revokeObjectURL(blobUrl); } catch (e) {}
        blobUrl = null;
      }
      try {
        el.onended = null;
        el.onerror = null;
        el.pause();
        el.removeAttribute('src');
        el.load();
      } catch (eStop) {
        // ignore
      }
      playNextAudio();
    }
    el.onended = function () {
      dbg('onended ct=' + el.currentTime + ' dur=' + el.duration);
      advance();
    };
    el.onerror = function () {
      dbg('onerror code=' + (el.error && el.error.code));
      console.error('[voice] tts audio error');
      revealNow();
      advance();
    };
    // Apply the playback-rate change only AFTER playback has actually started.
    // Setting playbackRate on a not-yet-loaded data-URI element wedges webOS
    // WebKit silently (play() never starts, no error) -> total silence.
    var rateApplied = false;
    function applyRate() {
      if (rateApplied || !item.speed || item.speed === 1.0) {
        return;
      }
      rateApplied = true;
      try {
        el.preservesPitch = true;
        el.mozPreservesPitch = true;
        el.webkitPreservesPitch = true;
      } catch (e) {
        // ignore unsupported property
      }
      try {
        el.playbackRate = item.speed;
      } catch (e) {
        // ignore: play at native speed
      }
    }
    el.addEventListener('loadedmetadata', function () {
      dbg('loadedmetadata dur=' + el.duration);
      // Prefer a duration-based watchdog: webOS WebKit often never fires
      // `ended` for data: URI <audio>, which left the overlay open forever.
      var dur = el.duration;
      if (isFinite(dur) && dur > 0) {
        if (watchdog) {
          clearTimeout(watchdog);
        }
        // Generous pad: under-reported duration used to fire early and cut
        // speech mid-word (stutter / incomplete read). Only advance if we
        // are truly near the end or stalled past the pad.
        function armDurationWatchdog(ms) {
          if (watchdog) {
            clearTimeout(watchdog);
          }
          watchdog = setTimeout(function () {
            if (advanced) {
              return;
            }
            try {
              var ct = el.currentTime || 0;
              var d = el.duration || dur;
              if (!el.paused && isFinite(d) && d > 0 && ct < d - 0.35) {
                // Still playing well before the end — reschedule, do not cut.
                dbg('duration watchdog deferred ct=' + ct + ' dur=' + d);
                armDurationWatchdog(1200);
                return;
              }
            } catch (eWd) {
              // fall through to advance
            }
            dbg('duration watchdog fired dur=' + dur);
            advance();
          }, ms);
        }
        armDurationWatchdog(Math.ceil(dur * 1000) + 2800);
      }
      // Seek to start once metadata is known (some builds open mid-buffer).
      try {
        el.currentTime = 0;
      } catch (eSeekMeta) {
        // ignore
      }
    });
    el.addEventListener('canplay', function () {
      dbg('canplay readyState=' + el.readyState);
      startPlay();
    });
    el.addEventListener('loadeddata', function () {
      startPlay();
    });
    el.addEventListener('playing', function () {
      dbg(
        'playing ct=' +
          el.currentTime +
          ' paused=' +
          el.paused +
          ' vol=' +
          el.volume +
          ' muted=' +
          el.muted
      );
      // If WebKit started past the intro, rewind once to the true start.
      try {
        if (!rewoundOnce && el.currentTime > 0.15) {
          rewoundOnce = true;
          dbg('rewind mid-start ct=' + el.currentTime + ' -> 0');
          el.currentTime = 0;
        }
      } catch (eRew) {
        // ignore
      }
      applyRate();
      // Text appears with audible speech — not during decode lag.
      revealNow();
    });
    el.addEventListener('stalled', function () {
      dbg('stalled');
    });
    // Some webOS builds fire `ended` unreliably; also listen for pause-at-end.
    el.addEventListener('pause', function () {
      try {
        if (el.ended || (isFinite(el.duration) && el.duration > 0 &&
            el.currentTime >= el.duration - 0.15)) {
          dbg('pause-at-end');
          advance();
        }
      } catch (e) {
        // ignore
      }
    });
    var startAttempted = false;
    function startPlay() {
      if (advanced) {
        return;
      }
      // Allow a re-kick while still paused at the start (first play() often
      // races ahead of canplay on webOS and must be retried).
      if (startAttempted) {
        try {
          if (!el.paused && el.currentTime > 0.05) {
            return;
          }
        } catch (eGate) {
          return;
        }
      }
      startAttempted = true;
      try {
        el.muted = false;
        el.volume = 1.0;
      } catch (eVol2) {
        // ignore
      }
      try {
        el.currentTime = 0;
      } catch (eSeek0) {
        // ignore — may only work after metadata
      }
      try {
        var p = el.play();
        if (p && typeof p.then === 'function') {
          p.then(function () {
            dbg(
              'play RESOLVED ct=' +
                el.currentTime +
                ' paused=' +
                el.paused +
                ' dur=' +
                el.duration
            );
            try {
              el.muted = false;
              el.volume = 1.0;
            } catch (eV3) {
              // ignore
            }
            try {
              if (!rewoundOnce && el.currentTime > 0.15) {
                rewoundOnce = true;
                dbg('rewind after play() ct=' + el.currentTime);
                el.currentTime = 0;
              }
            } catch (eRew2) {
              // ignore
            }
            if (!el.paused) {
              revealNow();
            }
          });
        }
        if (p && typeof p.catch === 'function') {
          p.catch(function (e) {
            dbg('play REJECTED ' + (e && e.name) + ':' + (e && e.message));
            console.error('[voice] tts play failed', e);
            // One retry after a short delay (LG UI often steals focus briefly).
            setTimeout(function () {
              try {
                el.muted = false;
                el.volume = 1.0;
                try {
                  el.currentTime = 0;
                } catch (eS) {
                  // ignore
                }
                var p2 = el.play();
                if (p2 && typeof p2.then === 'function') {
                  p2.then(function () {
                    revealNow();
                  }).catch(function () {
                    revealNow();
                    advance();
                  });
                } else {
                  revealNow();
                  advance();
                }
              } catch (e2) {
                revealNow();
                advance();
              }
            }, 350);
          });
        }
      } catch (e) {
        dbg('play THREW ' + (e && e.message));
        console.error('[voice] tts play threw', e);
        revealNow();
        advance();
      }
    }
    // Kick off loading; canplay/loadeddata call startPlay. Also attempt soon
    // after load — webOS often never fires canplay for data: URIs.
    try {
      el.load();
    } catch (e) {
      // ignore
    }
    if (el.readyState >= 2) {
      startPlay();
    }
    setTimeout(function () {
      if (!advanced && el && el.paused) {
        dbg('re-kick play readyState=' + el.readyState);
        startPlay();
      }
    }, 120);
    setTimeout(function () {
      if (!advanced && el && el.paused) {
        dbg('re-kick play#2 readyState=' + el.readyState);
        try {
          el.muted = false;
          el.volume = 1.0;
          try {
            el.currentTime = 0;
          } catch (eS2) {
            // ignore
          }
          var p3 = el.play();
          if (p3 && typeof p3.catch === 'function') {
            p3.catch(function () {});
          }
        } catch (eKick) {
          // ignore
        }
      }
    }, 400);
    // If still silent after 3.5s, skip this chunk so later ones can play.
    setTimeout(function () {
      if (!advanced && el && el.paused && (!isFinite(el.currentTime) || el.currentTime < 0.05)) {
        dbg('silent chunk skip mime=' + item.mime + ' readyState=' + el.readyState);
        revealNow();
        advance();
      }
    }, 3500);
    // Hard watchdog: never leave a single chunk stuck more than 12s.
    if (!watchdog) {
      watchdog = setTimeout(function () {
        dbg('hard watchdog fired readyState=' + el.readyState + ' paused=' + el.paused);
        revealNow();
        advance();
      }, 12000);
    }
  }

  function stopAnswerAudio() {
    dbg('stopAnswerAudio qlen=' + audioQueue.length + ' playing=' + audioPlaying);
    audioQueue = [];
    audioPlaying = false;
    if (answerAudioEl) {
      try {
        answerAudioEl.onended = null;
        answerAudioEl.onerror = null;
        answerAudioEl.pause();
        answerAudioEl.src = '';
      } catch (e) {
        // ignore
      }
      answerAudioEl = null;
    }
  }

  function ensureVoiceSubscription() {
    openBridge().catch(function (e) {
      console.error('[voice] ws open failed', e);
    });
  }

  document.addEventListener('keydown', function (ev) {
    if (ev.keyCode !== 461 && ev.key !== 'Back' && ev.key !== 'Escape') {
      return;
    }
    if (currentMode === 'voice') {
      handleVoiceEvent('sessionEnded');
      return;
    }
    closeApp();
  });

  document.addEventListener('webOSRelaunch', function (ev) {
    var params = (ev && ev.detail) || getLaunchParams();
    if (isVoiceLaunch(params)) {
      ensureVoiceUi();
      stageReady();
      // Focus-retry re-launches with source=launchhome mid-session. Never wipe
      // a question/answer already on screen — that was the "partial question"
      // bug users hit when the full utterance was already displayed.
      var hasContent =
        ((transcriptText && transcriptText.textContent) || '').trim() ||
        ((answerText && answerText.textContent) || '').trim() ||
        (answerBuffer || '').trim();
      if (!hasContent) {
        resetVoicePanels();
      }
      return;
    }
    // Not a voice launch (opened from the app list). Invalidate any pending
    // voice close so a late sessionEnded cannot act on a stale session.
    pendingClose = false;
    sessionUiGen += 1;
    closeGen = sessionUiGen;
    if (audioPlaying || audioQueue.length || (answerBuffer || '').trim()) {
      // Still speaking — keep the voice UI up until the answer finishes.
      ensureVoiceUi();
      stageReady();
      return;
    }
    stopAnswerAudio();
    closeApp();
  });

  // Recover if we ever paint with nothing visible (blank card).
  function recoverVisibleUi() {
    if (!overlayEl || overlayEl.classList.contains('hidden')) {
      if (isVoiceLaunch(getLaunchParams())) {
        ensureVoiceUi();
      } else {
        closeApp();
      }
    }
    stageReady();
  }

  if (isVoiceLaunch(getLaunchParams())) {
    resetVoicePanels();
    ensureVoiceUi();
  } else {
    closeApp();
  }
  stageReady();
  // PalmSystem.launchParams can arrive slightly after first paint on webOS.
  setTimeout(recoverVisibleUi, 200);
  setTimeout(recoverVisibleUi, 800);
})();