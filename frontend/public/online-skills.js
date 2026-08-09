(() => {
  const language = () => localStorage.getItem('aeroweaver-language') || 'en'
  const tr = (zh, en) => language() === 'zh' ? zh : en
  const api = '/api/skills/composite'

  function escapeHtml(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;')
  }

  function ensureDialog() {
    let dialog = document.getElementById('online-skill-dialog')
    if (dialog) return dialog
    dialog = document.createElement('dialog')
    dialog.id = 'online-skill-dialog'
    dialog.className = 'online-skill-dialog'
    dialog.innerHTML = `
      <form method="dialog" class="online-skill-shell">
        <header>
          <div>
            <strong data-copy="title"></strong>
            <span data-copy="subtitle"></span>
          </div>
          <button class="online-skill-icon" value="cancel" aria-label="Close">&times;</button>
        </header>
        <section class="online-skill-create">
          <label>
            <span data-copy="name"></span>
            <input id="online-skill-name" autocomplete="off" spellcheck="false" value="cross_formation_search" />
          </label>
          <label>
            <span data-copy="requirement"></span>
            <textarea id="online-skill-requirement" rows="4"></textarea>
          </label>
          <div class="online-skill-actions">
            <button type="button" id="online-skill-create" class="online-skill-primary"></button>
            <span id="online-skill-status" role="status"></span>
          </div>
        </section>
        <section class="online-skill-library">
          <div class="online-skill-library-head">
            <strong data-copy="library"></strong>
            <button type="button" id="online-skill-refresh" class="online-skill-secondary"></button>
          </div>
          <div id="online-skill-list"></div>
        </section>
      </form>`
    document.body.appendChild(dialog)
    dialog.querySelector('#online-skill-create').addEventListener('click', createSkill)
    dialog.querySelector('#online-skill-refresh').addEventListener('click', loadSkills)
    dialog.querySelector('#online-skill-list').addEventListener('click', handleListClick)
    return dialog
  }

  function updateCopy(dialog) {
    const copy = {
      title: tr('\u5728\u7ebf\u521b\u5efa\u53ef\u6267\u884c Skill', 'Create Executable Skill Online'),
      subtitle: tr(
        '\u53ea\u7ec4\u5408\u5df2\u6ce8\u518c\u80fd\u529b\uff0c\u521b\u5efa\u540e\u7acb\u5373\u53ef\u7528',
        'Compose registered capabilities and use the new Skill immediately',
      ),
      name: tr('Skill \u6807\u8bc6', 'Skill ID'),
      requirement: tr('\u80fd\u529b\u9700\u6c42', 'Behavior requirement'),
      library: tr('\u5df2\u521b\u5efa\u7684\u53ef\u6267\u884c Skills', 'Executable Skill Library'),
    }
    for (const [key, value] of Object.entries(copy)) {
      const node = dialog.querySelector(`[data-copy="${key}"]`)
      if (node) node.textContent = value
    }
    dialog.querySelector('#online-skill-create').textContent = tr('\u751f\u6210\u5e76\u6ce8\u518c', 'Generate & Register')
    dialog.querySelector('#online-skill-refresh').textContent = tr('\u5237\u65b0', 'Refresh')
    const requirement = dialog.querySelector('#online-skill-requirement')
    if (!requirement.dataset.edited) {
      requirement.value = tr(
        '\u8ba9\u6240\u6709\u6d3b\u52a8\u65e0\u4eba\u673a\u4fdd\u6301\u5341\u5b57\u5f62\u7f16\u961f\u5b8c\u6210\u533a\u57df\u641c\u7d22\u3002',
        'Keep all active UAVs in a cross formation while searching the selected area.',
      )
    }
    requirement.addEventListener('input', () => { requirement.dataset.edited = 'true' }, { once: true })
  }

  function openDialog() {
    const dialog = ensureDialog()
    updateCopy(dialog)
    dialog.showModal()
    loadSkills()
  }

  async function createSkill() {
    const dialog = ensureDialog()
    const button = dialog.querySelector('#online-skill-create')
    const status = dialog.querySelector('#online-skill-status')
    const name = dialog.querySelector('#online-skill-name').value.trim()
    const requirement = dialog.querySelector('#online-skill-requirement').value.trim()
    if (!name || !requirement) {
      status.textContent = tr('\u8bf7\u586b\u5199 Skill \u6807\u8bc6\u548c\u9700\u6c42\u3002', 'Enter a Skill ID and requirement.')
      status.dataset.state = 'error'
      return
    }
    button.disabled = true
    status.dataset.state = 'working'
    status.textContent = tr('\u6b63\u5728\u751f\u6210\u5b89\u5168\u7ec4\u5408\u914d\u65b9...', 'Generating a safe composite recipe...')
    try {
      const response = await fetch(`${api}/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, requirement }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`)
      status.dataset.state = 'success'
      status.textContent = tr(
        `\u5df2\u6ce8\u518c ${payload.name}\uff0c\u73b0\u5728\u53ef\u4ee5\u5728\u624b\u52a8\u6216 AI \u6a21\u5f0f\u4e2d\u8c03\u7528\u3002`,
        `${payload.name} is registered and available in Manual and AI modes.`,
      )
      await loadSkills()
    } catch (error) {
      status.dataset.state = 'error'
      status.textContent = error.message
    } finally {
      button.disabled = false
    }
  }

  async function loadSkills() {
    const list = ensureDialog().querySelector('#online-skill-list')
    list.innerHTML = `<p class="online-skill-empty">${escapeHtml(tr('\u6b63\u5728\u8bfb\u53d6...', 'Loading...'))}</p>`
    try {
      const response = await fetch(api)
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`)
      if (!payload.skills.length) {
        list.innerHTML = `<p class="online-skill-empty">${escapeHtml(tr('\u5c1a\u672a\u521b\u5efa\u53ef\u6267\u884c Skill\u3002', 'No executable Skills have been created.'))}</p>`
        return
      }
      list.innerHTML = payload.skills.map(skill => `
        <article class="online-skill-row">
          <div>
            <strong>${escapeHtml(skill.name)}</strong>
            <span>${escapeHtml(skill.description)}</span>
            <small>${escapeHtml(tr(`${skill.steps.length} \u4e2a\u7ec4\u5408\u6b65\u9aa4`, `${skill.steps.length} component step(s)`))}</small>
          </div>
          <div>
            <button type="button" class="online-skill-secondary" data-view="${escapeHtml(skill.name)}">${escapeHtml(tr('\u67e5\u770b', 'View'))}</button>
            <button type="button" class="online-skill-danger" data-remove="${escapeHtml(skill.name)}">${escapeHtml(tr('\u5220\u9664', 'Remove'))}</button>
          </div>
        </article>`).join('')
    } catch (error) {
      list.innerHTML = `<p class="online-skill-empty online-skill-error">${escapeHtml(error.message)}</p>`
    }
  }

  async function handleListClick(event) {
    const view = event.target.closest('[data-view]')
    const remove = event.target.closest('[data-remove]')
    if (view) {
      const response = await fetch(`${api}/${encodeURIComponent(view.dataset.view)}`)
      const payload = await response.json()
      if (payload.definition) {
        const details = window.open('', '_blank', 'width=760,height=720')
        details.document.write(`<pre style="white-space:pre-wrap;font:14px/1.5 monospace;padding:24px">${escapeHtml(JSON.stringify(payload.definition, null, 2))}</pre>`)
        details.document.close()
      }
    }
    if (remove) {
      const name = remove.dataset.remove
      if (!confirm(tr(`\u5220\u9664 ${name}\uff1f`, `Remove ${name}?`))) return
      const response = await fetch(`${api}/${encodeURIComponent(name)}`, { method: 'DELETE' })
      const payload = await response.json()
      if (!response.ok || !payload.ok) alert(payload.error || `HTTP ${response.status}`)
      await loadSkills()
    }
  }

  function ensureLauncher() {
    const form = document.querySelector('.soft-form')
    if (!form) return
    let button = form.querySelector('.online-skill-launch')
    if (!button) {
      button = document.createElement('button')
      button.type = 'button'
      button.className = 'online-skill-launch'
      button.addEventListener('click', openDialog)
      form.appendChild(button)
    }
    button.textContent = tr('\u521b\u5efa\u53ef\u6267\u884c Skill', 'Create Executable Skill')
  }

  const observer = new MutationObserver(ensureLauncher)
  observer.observe(document.documentElement, { childList: true, subtree: true })
  window.addEventListener('storage', ensureLauncher)
  ensureLauncher()
})()

