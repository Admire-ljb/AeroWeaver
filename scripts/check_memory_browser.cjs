const { chromium } = require('C:/Users/ljb/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')

const output = path.resolve('results/memory-browser/20260909-124908')

async function main() {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  try {
    await page.goto('http://10.61.3.7:5001', { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: 'Memory', exact: true }).click()
    const memory = page.getByRole('region', { name: 'Trajectory Memory', exact: true })
    await memory.getByRole('button', { name: 'Episodes 15', exact: true }).waitFor()
    assert.equal(await memory.locator('.tm-episode-row').count(), 15)
    assert.equal(await memory.getByText('Episodic', { exact: true }).count(), 0)
    await memory.screenshot({ path: path.join(output, 'episodes-desktop.png') })
    await memory.locator('.tm-episode-row').first().click()
    await memory.getByLabel('Agent filter').selectOption('UAV_1')
    await page.waitForFunction(() => {
      const cells = [...document.querySelectorAll('.tm-table tbody tr td:nth-child(2)')]
      return cells.length > 0 && cells.every(cell => cell.textContent.startsWith('UAV_1'))
    })
    await memory.getByLabel('Role filter').selectOption('leader')
    await memory.getByRole('button', { name: /^Inspect step/ }).first().click()
    await memory.getByRole('region', { name: 'Transition details' }).getByText('Write only', { exact: true }).waitFor()
    assert.ok((await memory.locator('.tm-table-scroll').boundingBox()).height >= 100)
    await memory.screenshot({ path: path.join(output, 'episode-detail-desktop.png') })
    await memory.getByLabel('Role filter').selectOption('forager')
    await memory.getByText('No matching records', { exact: true }).waitFor()
    await memory.getByRole('button', { name: 'Transitions 616', exact: true }).click()
    await memory.getByRole('button', { name: 'Next page', exact: true }).click()
    await memory.getByText('26-50 / 616', { exact: true }).waitFor()
    await memory.getByRole('button', { name: 'Failed intervals 41', exact: true }).click()
    await memory.getByRole('button', { name: /^Inspect step/ }).first().click()
    const details = memory.getByRole('region', { name: 'Transition details' })
    await details.getByText("Skill or target is outside this role's local task options", { exact: true }).waitFor()
    const downloadPromise = page.waitForEvent('download')
    await details.getByRole('button', { name: 'Download record JSON', exact: true }).click()
    const download = await downloadPromise
    const downloadedPath = path.join(output, 'downloaded-transition.json')
    await download.saveAs(downloadedPath)
    const record = JSON.parse(fs.readFileSync(downloadedPath))
    assert.equal(record.success, false)
    assert.equal(record.return_finalized, true)
    assert.equal(record.metadata.reuse_allowed, false)
    await memory.getByLabel('Search memory').fill('definitely-no-such-episode')
    await memory.getByText('No matching records', { exact: true }).waitFor()
    await memory.getByRole('button', { name: 'Episodes 15', exact: true }).click()
    await page.setViewportSize({ width: 390, height: 844 })
    await memory.locator('.tm-episode-row').first().waitFor()
    await memory.scrollIntoViewIfNeeded()
    await memory.screenshot({ path: path.join(output, 'episodes-mobile.png') })
    const layout = await memory.evaluate(el => {
      const box = el.getBoundingClientRect()
      return { width: box.width, viewport: innerWidth, horizontalOverflow: el.scrollWidth > el.clientWidth + 2 }
    })
    assert.equal(layout.horizontalOverflow, false)
    await page.route('**/api/memory/trajectory-stats', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ ok: false, error: 'Test unavailable' }) }))
    await memory.getByRole('button', { name: 'Refresh memory', exact: true }).click()
    await memory.getByRole('alert').filter({ hasText: 'Test unavailable' }).waitFor()
    await page.unroute('**/api/memory/trajectory-stats')
    await memory.getByRole('button', { name: 'Refresh memory', exact: true }).click()
    await memory.getByRole('button', { name: 'Episodes 15', exact: true }).waitFor()
    assert.deepEqual(errors, [])
    const report = { passed: true, checks: ['episode list', 'agent filter', 'role filter', 'no-result state', 'pagination', 'failed intervals', 'transition inspection', 'JSON download', 'request error display'], layout, pageErrors: errors }
    fs.writeFileSync(path.join(output, 'browser-check.json'), JSON.stringify(report, null, 2))
    console.log(JSON.stringify(report, null, 2))
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'browser-failure.png'), fullPage: true })
    console.error(await page.locator('body').innerText())
    throw error
  } finally {
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
