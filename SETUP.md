# Sätta upp molnagenten

När det här är klart kör agenten själv varje vardag, oavsett om din dator är
på, och du når dashboarden från vilken telefon eller dator som helst.

Räkna med ungefär 30 minuter. Du behöver inte kunna programmera — men du kommer
att klicka runt en del i GitHubs inställningar.

---

## Steg 1 — GitHub-konto

Skapa ett konto på [github.com](https://github.com) om du inte har ett. Gratis.

## Steg 2 — Skapa förrådet

1. Klicka **New repository**
2. Namn: `omx60-agent`
3. Välj **Public**
4. Kryssa **inte** i något av tilläggen (README, .gitignore, licens)
5. **Create repository**

> **Om Public känns fel:** GitHub Pages kräver Public för att vara gratis.
> Innehållet är fiktiv pappershandel, ingen personlig eller finansiell
> information, och adressen är inte sökbar om du inte länkar den. Vill du ha
> riktig inloggning finns ett alternativ längst ned under *Privat dashboard*.

## Steg 3 — Ladda upp filerna

På förrådets startsida, klicka **uploading an existing file**.

Dra in **allt innehåll i mappen `cloud`** — alltså `scripts`, `config`,
`requirements.txt`, `README.md`, och den dolda mappen `.github`.

> **Viktigt:** `.github`-mappen är dold i Utforskaren. Slå på *Visa → Dolda
> objekt* i Windows först, annars följer arbetsflödet inte med och ingenting
> kommer att köras.

Klicka **Commit changes**.

## Steg 4 — API-nyckel för researchdelen

1. Gå till [console.anthropic.com](https://console.anthropic.com) → **API Keys**
   → **Create Key**. Kopiera nyckeln.
2. I ditt GitHub-förråd: **Settings → Secrets and variables → Actions**
   → **New repository secret**
3. Namn: `ANTHROPIC_API_KEY`. Klistra in nyckeln som värde. **Add secret**.

> Klistra aldrig in nyckeln i en chatt, i en fil i förrådet eller någon
> annanstans. GitHub-hemligheten är rätt ställe — den syns inte ens för dig
> efteråt, och den loggas aldrig i körningarna.

Du behöver fylla på med en mindre summa under **Billing** i konsolen för att
API:et ska svara. Kostnaden för det här flödet ligger på storleksordningen
någon krona per körning, alltså tiotals kronor i månaden. Kontrollera faktisk
förbrukning i konsolen efter första veckan.

## Steg 5 — Ge agenten skrivrättigheter

**Settings → Actions → General** → längst ned under **Workflow permissions**:

- Välj **Read and write permissions**
- **Save**

Utan det här kan agenten inte spara sitt minne mellan körningar, och lärandet
slutar fungera. Det är det vanligaste felet i hela uppsättningen.

## Steg 6 — Slå på Pages

**Settings → Pages** → under **Source**, välj **GitHub Actions**.

## Steg 7 — Första körningen

Gå till fliken **Actions** → **OMX60-agenten** → **Run workflow**. Välj läge
`morgon` och kör.

Första gången hämtas två års historik för runt 70 bolag, vilket tar några
minuter. Efterföljande körningar tar under en minut.

När den är klar hittar du din adress under **Settings → Pages**. Den ser ut
ungefär så här:

```
https://<ditt-användarnamn>.github.io/omx60-agent/
```

## Steg 8 — Lägg till i mobilen

Öppna adressen i mobilen och lägg till på hemskärmen:

- **iPhone:** Dela-knappen → *Lägg till på hemskärmen*
- **Android:** menyn ⋮ → *Lägg till på startskärmen*

Sidan öppnas då i helskärm utan webbläsarram, som en vanlig app.

---

## Vad som händer sedan

| Tid (svensk) | Vad agenten gör |
|---|---|
| 07:30 vardagar | Hämtar kurser, gör research, räknar signaler, öppnar positioner |
| 19:05 vardagar | Hämtar stängningskurser, stänger positioner, mäter utfall, justerar vikter |

Båda körningarna uppdaterar dashboarden och sparar agentens minne i förrådet.
Tiderna är satta i UTC och ligger rätt även efter sommartidsomställningen — på
vintern blir de 06:30 respektive 18:05.

---

## Om något går fel

**Ingenting körs på schemat**
GitHub stänger av schemalagda arbetsflöden i förråd som varit inaktiva i 60
dagar. Agentens egna sparningar räknas inte alltid som aktivitet. Gör en liten
ändring i README då och då, eller kör manuellt via **Run workflow**.

**Körningen misslyckas på "Spara agentens minne"**
Steg 5 är inte gjort. Gå tillbaka och sätt *Read and write permissions*.

**Research-steget hoppas över**
Det är avsiktligt satt till `continue-on-error`, så en misslyckad research
stoppar inte resten. Kolla loggen: oftast saknad eller slut API-kredit.
Modellen kör vidare på enbart teknisk data de dagarna, med lägre konfidens.

**Många bolag saknar data**
Titta i `config/universe_active.json` för att se vilka som föll bort. Tickers
ändras vid namnbyten och uppköp. Justera `config/universe.json` och kör om.

**Schemalagd tid stämmer inte**
GitHub Actions kör cron i UTC och kan bli några tiotals minuter försenad vid
hög belastning. Det påverkar inte analysen — kurserna är dagsdata.

---

## Privat dashboard

Vill du att bara du ska komma åt sidan, byt ut steg 6 mot Cloudflare Pages:

1. Skapa gratiskonto på [cloudflare.com](https://cloudflare.com)
2. **Workers & Pages → Create → Pages → Connect to Git**, välj förrådet
3. Build output directory: `docs`
4. Under **Zero Trust → Access → Applications**, lägg till sidan som en
   applikation och sätt en policy som bara släpper in din mejladress

Då kan förrådet vara privat och sidan kräver inloggning. Gratisnivån räcker
gott. Det är fler steg, men det är den enda vägen till riktig åtkomstkontroll
utan att betala.
