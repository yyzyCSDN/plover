# Command Line Reference

This page describes all the options available to run Plover from the command
line. The command is available under slightly different names on each platform,
relative to your installation directory:

- Windows: `C:\Program Files (x86)\Open Steno Project\Plover 4.0.0\plover_console.exe`
- macOS: `/Applications/Plover.app/Contents/MacOS/Plover`
- Linux: `plover.AppImage`

All of the above commands will be referred to as `plover` below.

```{program} plover
```

```{option} --help
Display the help text and then exit.
```

```{option} --version
Display the running Plover version and then exit.
```

````{option} -g <gui>
Specify the GUI system to use. The options are as follows:

```{describe} qt
Run the built-in Qt-based GUI. This is the default value.
```

```{describe} none
Run Plover headless, i.e. without a GUI. You'll only be able to control
Plover from the command line or with steno commands.
````

```{option} -l <level>, --log-level <level>
Change the minimum level of logs being shown on standard output. Options
are `debug`, `info`, `warning`, and `error`. By default, no logs
are displayed.
```

```{option} -s <script>, --script <script>
Use another console script as the main entrypoint. This passes the
remainder of the command line arguments to the console script. For example,
to run the plugin installer:

    plover -s plover_plugins

Pass `-s` without an argument to show the list of console scripts.
```

(plugin-installer)=

## Plugin Installer

The plugin installer can be accessed through the `plover_plugins` console
script:

    plover -s plover_plugins

This is essentially a wrapper around `pip`; you can run commands to install
and remove packages just like on a normal Python installation. For example, to
install and uninstall the `plover-treal` plugin:

    plover -s plover_plugins install plover-treal
    plover -s plover_plugins uninstall plover-treal

To list all of the plugins available on your system:

    plover -s plover_plugins list

To install a plugin you are working on locally:

    cd plover_myplugin
    plover -s plover_plugins install -e .

```{note}
Some users have had issues loading the plugin list with the Plugin Manager;
this seems to have something to do with an outdated version of another
package. This can be fixed with the following command:

    plover -s plover_plugins install --disable-pip-version-check --upgrade Pygments
```

(send-command)=

## Sending Commands

The `plover_send_command` console script can be used to send commands to an
existing Plover instance:

    plover -s plover_send_command

It takes the command name and parameters as an argument, written as they would
be in a dictionary definition. For example, to send the command
`{plover:toggle}`, you can invoke this script as follows:

    plover -s plover_send_command toggle

The following built-in commands are available:

```{describe} resume
Enable steno output.
```

```{describe} suspend
Disable steno output.
```

```{describe} toggle
Toggle steno output on or off.
```

```{describe} quit
Quit Plover.
```

```{describe} focus
Bring the Plover window to the foreground.
```

```{describe} configure
Open the configuration dialog.
```

```{describe} add_translation
Open the Add Translation dialog.
```

```{describe} lookup
Open the Lookup dialog.
```

```{describe} suggestions
Open the Suggestions dialog.
```

```{describe} reconnect_machine
Disconnect and reconnect the current machine. Equivalent to pressing the
*Reconnect* button in the main window.
```

```{describe} reload_dictionaries
Reload all dictionaries from disk without resetting the machine connection.
Useful for picking up changes made to dictionary files outside of Plover.
```

```{describe} reload_config
Reload the configuration file from disk and re-apply all settings, including
dictionaries, machine type, keymap, system, and enabled extensions. Useful
when the configuration file has been changed externally (for example, when
syncing settings across machines).
```

````{describe} set_config:<key>:<value>[,<key>:<value>,...]
Set one or more configuration options. Keys and string values should be
quoted with single quotes. For example:

    plover -s plover_send_command "set_config:'translation_frame_opacity':90"
````

(dictionary-audit)=

## Dictionary Compatibility Audit

The `plover_dict_audit` console script performs an offline, exhaustive
compatibility check between an old and a new plain dictionary stack. It
explores every stroke sequence over a finite raw stroke alphabet and compares
the text produced after each stroke, using an isolated native translator and
formatter: no keyboard is connected, no external operation is executed and the
audited dictionaries are never modified.

    plover -s plover_dict_audit audit_spec.json [--json report.json]

The audit specification is a JSON document:

```json
{
  "old": [{"path": "old/main.json", "enabled": true}],
  "new": [{"path": "new/main.json", "enabled": true}],
  "strokes": ["S", "T", "E", "*"],
  "undo_levels": 100,
  "start_attached": false,
  "start_capitalized": false,
  "space_char": " ",
  "space_placement": "Before Output",
  "budget": {
    "max_states": 100000,
    "max_transitions": 2000000,
    "timeout_seconds": 600
  }
}
```

Dictionary entries may also be provided inline as
`{"mappings": {"S": "some text"}}` instead of `path`. The scope is the native
English Stenotype pure-text behavior: multi-stroke re-segmentation, prefixes
and implicit suffixes, attach/glue (including unbounded loops), orthography,
forward case and mode, punctuation, word-end, carry capitalization, look-ahead
and undo within the recoverable history. Plugin metas and macros,
retro-case/retro-currency, key combinations, engine commands and undo past the
recoverable history make the affected branch leave the audit domain; an old
side domain exit excludes the branch from the guarantee, while a new side
domain exit on a covered branch is an incompatibility.

The verdict is one of:

```{describe} compatible
Every reachable semantic state and every alphabet successor was covered and
the text after each stroke matches. The report contains the coverage evidence
(state, transition and merge counts, reached depth and out-of-domain branches)
and the suffix horizon used to merge unbounded attach/glue loops.
```

```{describe} incompatible
The shortest counterexample is reported, together with the first differing
stroke (or the new side domain exit), and confirmed by an independent isolated
native replay.
```

```{describe} inconclusive
The configured resource budget was exhausted before the reachable product
state space could be closed. No compatibility claim is made; the partial
coverage evidence is still reported.
```

Exit codes are `0` (compatible), `1` (incompatible), `2` (inconclusive), `3`
(specification error) and `4` (internal replay verification failure).
