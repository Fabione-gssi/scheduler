const { chromium } = require('playwright');

const APP_URL = process.env.APP_URL || 'https://scheduler-ailab.streamlit.app/';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  console.log("Opening:", APP_URL);
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded', timeout: 120000 });

  // Il bottone di wake può apparire con quel testo esatto
  const wakeButton = page.getByRole('button', { name: 'Yes, get this app wake up!' });

  if (await wakeButton.count()) {
    console.log("Sleep screen detected. Clicking wake button...");
    await wakeButton.first().click({ timeout: 30000 });
  } else {
    console.log("No sleep button found. App likely awake.");
  }

  // Attendi che l'app si carichi: Streamlit spesso mostra elementi standard, es. title/h1 o sidebar.
  // Usiamo un paio di retry, senza assumere selettori specifici della tua app.
  for (let i = 1; i <= 4; i++) {
    try {
      await page.waitForLoadState('networkidle', { timeout: 60000 });
      // prova un reload soft per verificare stabilità
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 120000 });
      console.log(`Warm reload ${i}/4 done`);
      break;
    } catch (e) {
      console.log(`Retry ${i}/4:`, e.message);
      await page.waitForTimeout(5000);
    }
  }

  await browser.close();
  console.log("Done.");
})();
