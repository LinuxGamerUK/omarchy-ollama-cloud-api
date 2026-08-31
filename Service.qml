import QtQuick
import Quickshell
import Quickshell.Io

// API-key collector for the stock omarchy.agents panel.
// Forked from omarchy-ollama-cloud (styles01, MIT): instead of rendering
// ollama.com with a headless Chromium and browser cookies, it calls the
// official https://ollama.com/api/usage endpoint with the user's personal
// API key (user-owned 0600 file or environment; never argv).
//
// The collector writes ~/.local/state/omarchy/agents/usage/ollama.json;
// the built-in AI button already watches that directory and draws whatever
// appears there.
Item {
  id: root

  property var manifest: null
  property var shell: null

  readonly property string pluginDir: manifest && manifest.__sourceDir ? String(manifest.__sourceDir) : ""
  readonly property string collector: pluginDir + "/collect.py"
  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || (home + "/.local/state")
  readonly property string claudeRecord: stateHome + "/omarchy/agents/usage/claude.json"

  // ── Deadlines ────────────────────────────────────────────────────────
  // `timeout -k 2 N` is primary (process-group aware: TERM then KILL); the
  // QML watchdog is the backup that SIGKILLs if timeout itself hangs. The
  // collector's own urllib timeout (20s) fires first in normal failure.
  readonly property int collectTimeoutSec: 30
  readonly property int collectWatchdogMs: 35000

  function collect(force) {
    if (pluginDir === "" || collectProcess.running) return
    // `set -o pipefail` preserves the collector's exit code; `head -c` caps
    // the merged output at the OS pipe so SplitParser's buffer stays
    // bounded even if something upstream misbehaves.
    var flags = force === true ? " --force" : ""
    var inner = "python3 " + collector + flags + " --write 2>&1 | head -c 2048"
    collectProcess.command = ["timeout", "-k", "2", "" + collectTimeoutSec,
      "bash", "-c", "set -o pipefail; " + inner]
    collectProcess.running = true
    collectWatchdog.restart()
  }

  function clearRecord() {
    if (pluginDir === "") return
    clearProcess.command = ["timeout", "-k", "2", "8", "python3", collector, "--clear"]
    clearProcess.running = true
  }

  // Ollama Cloud usage changes slowly; 15 minutes is plenty and keeps the
  // API call cheap.
  Timer {
    interval: 900000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.collect(false)
  }

  // Stock panel refresh rewrites claude.json. Use that as a cue so Ollama
  // updates when the user hits r, not only on this timer.
  FileView {
    path: root.claudeRecord
    watchChanges: true
    printErrors: false
    onFileChanged: root.collect(false)
  }

  Process {
    id: collectProcess
    running: false
    stdout: SplitParser {
      onRead: function(line) { console.warn("ollama-cloud-usage", line) }
    }
    onExited: collectWatchdog.stop()
  }

  Process {
    id: clearProcess
    running: false
    stderr: StdioCollector { waitForEnd: true }
  }

  // Backup deadline: only fires if `timeout` itself is broken or wedged.
  Timer {
    id: collectWatchdog
    interval: root.collectWatchdogMs
    repeat: false
    onTriggered: if (collectProcess.running) {
      collectProcess.signal(9)
      console.warn("ollama-cloud-usage", "watchdog killed a hung collector")
    }
  }

  Component.onDestruction: root.clearRecord()
}