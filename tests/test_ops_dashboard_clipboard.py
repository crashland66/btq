from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ops_dashboard.layout import html_page


def _dashboard_page_script() -> str:
    page = html_page("Clipboard test", "<button>Test</button>", active_section="home")
    marker = "function copyTextToClipboard(text)"
    start = page.index(marker)
    script_start = page.rfind("<script>", 0, start)
    script_end = page.find("</script>", start)
    return page[script_start + len("<script>") : script_end]


def _dashboard_function(script: str, name: str) -> str:
    marker = f"function {name}"
    start = script.index(marker)
    brace_start = script.index("{", start)
    depth = 0
    for index in range(brace_start, len(script)):
        char = script[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return script[start : index + 1]
    raise AssertionError(f"{name} function was not closed")


def _dashboard_copy_helpers() -> str:
    script = _dashboard_page_script()
    return "\n".join(
        [
            _dashboard_function(script, "copyTextToClipboard(text)"),
            _dashboard_function(script, "fallbackCopy(text)"),
        ]
    )


def _dashboard_copy_block() -> str:
    script = _dashboard_page_script()
    start = script.index("document.querySelectorAll('[data-copy-target], [data-copy-value]')")
    end = script.index("const shell = document.querySelector('.admin-shell');", start)
    return script[start:end]


def test_dashboard_clipboard_fallback_emits_in_viewport_textarea() -> None:
    script = _dashboard_page_script()

    assert (
        "if (navigator.clipboard && navigator.clipboard.writeText) {\n"
        "          return navigator.clipboard.writeText(text).catch(function () { return fallbackCopy(text); });\n"
        "        }"
    ) in script
    assert "function fallbackCopy(text)" in script
    assert "return fallbackCopy(text);" in script
    assert "ta.style.position = 'fixed';" in script
    assert "ta.style.top = '0';" in script
    assert "ta.style.left = '0';" in script
    assert "ta.style.width = '1px';" in script
    assert "ta.style.height = '1px';" in script
    assert "ta.style.opacity = '0';" in script
    assert "ta.focus();" in script
    assert "ta.select();" in script
    assert "ta.setSelectionRange(0, text.length);" in script
    assert "reject(new Error('execCommand returned false'))" in script
    assert "document.body.removeChild(ta);" in script
    assert "-10000px" not in script


def test_dashboard_clipboard_helper_node_behavior(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    script = _dashboard_page_script()
    function_source = _dashboard_copy_helpers()
    syntax_check = tmp_path / "copy_helper.js"
    syntax_check.write_text(function_source, encoding="utf-8")
    checked = subprocess.run([node, "--check", str(syntax_check)], text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr
    emitted_syntax_check = tmp_path / "dashboard_script.js"
    emitted_syntax_check.write_text(script, encoding="utf-8")
    checked = subprocess.run([node, "--check", str(emitted_syntax_check)], text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr

    behavior_check = tmp_path / "copy_helper_behavior.js"
    behavior_check.write_text(
        f"""
const assert = require('assert');

{function_source}

function setGlobal(name, value) {{
  Object.defineProperty(globalThis, name, {{
    value,
    configurable: true,
    writable: true,
  }});
}}

function makeDocument(execBehavior) {{
  let activeElement = null;
  const appended = [];
  const removed = [];
  return {{
    appended,
    removed,
    body: {{
      appendChild(element) {{
        appended.push(element);
      }},
      removeChild(element) {{
        removed.push(element);
      }},
    }},
    createElement(tagName) {{
      assert.strictEqual(tagName, 'textarea');
      return {{
        value: '',
        attributes: {{}},
        style: {{}},
        focusCalls: 0,
        selectCalls: 0,
        selectionRange: null,
        setAttribute(name, value) {{
          this.attributes[name] = value;
        }},
        focus() {{
          this.focusCalls += 1;
          activeElement = this;
        }},
        select() {{
          this.selectCalls += 1;
        }},
        setSelectionRange(start, end) {{
          this.selectionRange = [start, end];
        }},
      }};
    }},
    execCommand(command) {{
      assert.strictEqual(command, 'copy');
      return execBehavior(activeElement, appended[appended.length - 1]);
    }},
  }};
}}

(async () => {{
  let clipboard = '';
  setGlobal('navigator', {{}});
  setGlobal('document', makeDocument((activeElement) => {{
    clipboard = activeElement.value;
    return true;
  }}));

  await copyTextToClipboard('first-token');
  assert.strictEqual(clipboard, 'first-token');
  await copyTextToClipboard('second-token');
  assert.strictEqual(clipboard, 'second-token');
  assert.strictEqual(document.appended.length, 2);
  assert.strictEqual(document.removed.length, 2);

  const firstTextarea = document.removed[0];
  assert.strictEqual(firstTextarea.style.position, 'fixed');
  assert.strictEqual(firstTextarea.style.top, '0');
  assert.strictEqual(firstTextarea.style.left, '0');
  assert.strictEqual(firstTextarea.style.width, '1px');
  assert.strictEqual(firstTextarea.style.height, '1px');
  assert.strictEqual(firstTextarea.style.opacity, '0');
  assert.strictEqual(firstTextarea.focusCalls, 1);
  assert.strictEqual(firstTextarea.selectCalls, 1);
  assert.deepStrictEqual(firstTextarea.selectionRange, [0, 'first-token'.length]);

  setGlobal('document', makeDocument(() => false));
  await assert.rejects(
    () => copyTextToClipboard('will-fail'),
    /execCommand returned false/
  );
  assert.strictEqual(document.removed.length, 1);

  clipboard = 'prior-token';
  const rejectingWriteTextCalls = [];
  setGlobal('navigator', {{
    clipboard: {{
      writeText: async (text) => {{
        rejectingWriteTextCalls.push(text);
        throw new Error('Document is not focused');
      }},
    }},
  }});
  setGlobal('document', makeDocument((activeElement) => {{
    clipboard = activeElement.value;
    return true;
  }}));
  await copyTextToClipboard('current-token');
  assert.deepStrictEqual(rejectingWriteTextCalls, ['current-token']);
  assert.strictEqual(clipboard, 'current-token');
  assert.strictEqual(document.appended.length, 1);
  assert.strictEqual(document.removed.length, 1);

  setGlobal('navigator', {{
    clipboard: {{
      writeText: async () => {{
        throw new Error('Document is not focused');
      }},
    }},
  }});
  setGlobal('document', makeDocument(() => false));
  await assert.rejects(
    () => copyTextToClipboard('will-still-fail'),
    /execCommand returned false/
  );
  assert.strictEqual(document.appended.length, 1);
  assert.strictEqual(document.removed.length, 1);

  const writeTextCalls = [];
  setGlobal('navigator', {{
    clipboard: {{
      writeText: async (text) => {{
        writeTextCalls.push(text);
      }},
    }},
  }});
  setGlobal('document', makeDocument(() => {{
    throw new Error('fallback should not run');
  }}));
  await copyTextToClipboard('secure-token');
  assert.deepStrictEqual(writeTextCalls, ['secure-token']);
  assert.strictEqual(document.appended.length, 0);
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
""",
        encoding="utf-8",
    )
    checked = subprocess.run([node, str(behavior_check)], text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr


def test_dashboard_clipboard_click_handler_persistent_failure(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    copy_block = _dashboard_copy_block()
    behavior_check = tmp_path / "copy_handler_behavior.js"
    behavior_check.write_text(
        f"""
const assert = require('assert');

function makeClassList() {{
  const values = new Set();
  return {{
    add(name) {{ values.add(name); }},
    remove(name) {{ values.delete(name); }},
    contains(name) {{ return values.has(name); }},
  }};
}}

const button = {{
  textContent: 'Copy',
  disabled: false,
  attributes: {{
    'data-copy-value': 'current-token',
  }},
  classList: makeClassList(),
  listeners: {{}},
  getAttribute(name) {{
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }},
  setAttribute(name, value) {{
    this.attributes[name] = value;
  }},
  addEventListener(name, callback) {{
    this.listeners[name] = callback;
  }},
}};

async function flushPromises() {{
  for (let index = 0; index < 10; index += 1) {{
    await Promise.resolve();
  }}
}}

let timers = [];
let appended = [];
let removed = [];
Object.defineProperty(globalThis, 'setTimeout', {{
  value: (callback, delay) => {{
    timers.push({{ callback, delay }});
    return timers.length;
  }},
  configurable: true,
}});
Object.defineProperty(globalThis, 'navigator', {{
  value: {{
    clipboard: {{
      writeText: async () => {{
        throw new Error('Document is not focused');
      }},
    }},
  }},
  configurable: true,
}});
Object.defineProperty(globalThis, 'document', {{
  value: {{
    body: {{
      appendChild(element) {{
        appended.push(element);
      }},
      removeChild(element) {{
        removed.push(element);
      }},
    }},
    querySelectorAll(selector) {{
      assert.strictEqual(selector, '[data-copy-target], [data-copy-value]');
      return [button];
    }},
    querySelector() {{
      return null;
    }},
    createElement(tagName) {{
      assert.strictEqual(tagName, 'textarea');
      return {{
        value: '',
        attributes: {{}},
        style: {{}},
        setAttribute(name, value) {{
          this.attributes[name] = value;
        }},
        focus() {{}},
        select() {{}},
        setSelectionRange() {{}},
      }};
    }},
    execCommand(command) {{
      assert.strictEqual(command, 'copy');
      return false;
    }},
  }},
  configurable: true,
}});

{copy_block}

(async () => {{
  button.listeners.click();
  await flushPromises();

  assert.strictEqual(appended.length, 1);
  assert.strictEqual(removed.length, 1);
  assert.strictEqual(button.textContent, 'Copy failed');
  assert.strictEqual(button.disabled, false);
  assert.strictEqual(button.classList.contains('is-copy-failed'), true);
  assert.strictEqual(timers.length, 0);
  assert.strictEqual(button.getAttribute('data-copy-original-label'), 'Copy');

  button.listeners.click();
  assert.strictEqual(button.textContent, 'Copy');
  assert.strictEqual(button.classList.contains('is-copy-failed'), false);
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
""",
        encoding="utf-8",
    )
    checked = subprocess.run([node, str(behavior_check)], text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr
