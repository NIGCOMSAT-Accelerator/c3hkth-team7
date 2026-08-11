/**
 * Mobile number entry: a country picker with flag and dialling code, beside a local
 * number input.
 *
 * ## Why this replaced a single free-text field
 *
 * The old field was one input with the placeholder `0803 123 4567` and the hint "a local
 * number is fine", and the backend turned a bare local number into `+234…`. That is
 * correct for Nigeria and silently wrong everywhere else on the continent: a Kenyan
 * entering `0722…` got `+234722…`, which is a valid-looking Nigerian number belonging to
 * someone else. It would pass validation, store cleanly, and fail only when an alert was
 * dispatched — or worse, deliver a flood warning to a stranger.
 *
 * Making the country explicit removes the guess. It is also the cheapest possible signal
 * that this is a continental platform rather than a Nigerian one.
 *
 * ## Scope: all 54 African countries, and why not a shorter list
 *
 * The instinct is to list only "covered" countries. Checked against the actual data path,
 * that set is the whole continent:
 *
 *   * **Sentinel-1 / Sentinel-2** — global acquisition.
 *   * **CHIRPS** — 50°S to 50°N. Africa spans roughly 37°N (Tunisia) to 35°S (South
 *     Africa), so the continent sits entirely inside the band, with margin.
 *   * **WorldPop, WorldCover, SoilGrids, Copernicus DEM** — global.
 *   * **Malaria Atlas** — Africa is its core coverage.
 *
 * So no African country is excluded by Earth Observation. Trimming the list would
 * misrepresent the platform as narrower than it is, and a subscriber whose country is
 * missing reads that as "not for me" — an unrecoverable impression at signup.
 *
 * What genuinely varies is **delivery**, and that varies by subscriber rather than by
 * country: terrestrial channels need a working network wherever the subscriber is, and
 * the NIGCOMSAT-1R Ku-band footprint is strongest over West and Central Africa. Neither
 * belongs in a country dropdown — a country is not a channel, and encoding a footprint as
 * a hard list would refuse signups the terrestrial channels serve perfectly well.
 *
 * ## Composition rather than client state
 *
 * The select and the input are separate named fields (`phone_country`, `phone_local`) and
 * the Server Action joins them. The alternative — a client component mirroring both into
 * a hidden `phone` input — needs JavaScript to produce a correct submission, so a slow or
 * failed bundle load on a low-end Android would submit an empty number silently. Joining
 * server-side means this whole component ships zero JavaScript and works before hydration.
 */

/**
 * Dialling codes for all 54 African Union member states.
 *
 * Nigeria is first and is the default — it is the launch market, and a picker that opens
 * on the modal answer saves most subscribers an interaction. The rest are alphabetical,
 * because any other ordering (population, "priority") is a judgement the user has to
 * decode before they can scan.
 *
 * `flag` is an emoji, not an image: an SVG sprite for 54 flags is ~40 KB for decoration,
 * and the ZeroRate CDN pays for every byte. Where the platform has no emoji flag font
 * (notably Windows Chrome) the glyph falls back to the two-letter code, which is still
 * meaningful — and the country name and dial code sit right beside it either way, so
 * nothing depends on the flag rendering.
 */
export const AFRICAN_DIAL_CODES: {
  iso: string;
  name: string;
  dial: string;
  flag: string;
}[] = [
  { iso: "NG", name: "Nigeria", dial: "+234", flag: "🇳🇬" },
  { iso: "DZ", name: "Algeria", dial: "+213", flag: "🇩🇿" },
  { iso: "AO", name: "Angola", dial: "+244", flag: "🇦🇴" },
  { iso: "BJ", name: "Benin", dial: "+229", flag: "🇧🇯" },
  { iso: "BW", name: "Botswana", dial: "+267", flag: "🇧🇼" },
  { iso: "BF", name: "Burkina Faso", dial: "+226", flag: "🇧🇫" },
  { iso: "BI", name: "Burundi", dial: "+257", flag: "🇧🇮" },
  { iso: "CV", name: "Cabo Verde", dial: "+238", flag: "🇨🇻" },
  { iso: "CM", name: "Cameroon", dial: "+237", flag: "🇨🇲" },
  { iso: "CF", name: "Central African Republic", dial: "+236", flag: "🇨🇫" },
  { iso: "TD", name: "Chad", dial: "+235", flag: "🇹🇩" },
  { iso: "KM", name: "Comoros", dial: "+269", flag: "🇰🇲" },
  { iso: "CG", name: "Congo — Republic", dial: "+242", flag: "🇨🇬" },
  { iso: "CD", name: "Congo — DR", dial: "+243", flag: "🇨🇩" },
  { iso: "CI", name: "Côte d’Ivoire", dial: "+225", flag: "🇨🇮" },
  { iso: "DJ", name: "Djibouti", dial: "+253", flag: "🇩🇯" },
  { iso: "EG", name: "Egypt", dial: "+20", flag: "🇪🇬" },
  { iso: "GQ", name: "Equatorial Guinea", dial: "+240", flag: "🇬🇶" },
  { iso: "ER", name: "Eritrea", dial: "+291", flag: "🇪🇷" },
  { iso: "SZ", name: "Eswatini", dial: "+268", flag: "🇸🇿" },
  { iso: "ET", name: "Ethiopia", dial: "+251", flag: "🇪🇹" },
  { iso: "GA", name: "Gabon", dial: "+241", flag: "🇬🇦" },
  { iso: "GM", name: "Gambia", dial: "+220", flag: "🇬🇲" },
  { iso: "GH", name: "Ghana", dial: "+233", flag: "🇬🇭" },
  { iso: "GN", name: "Guinea", dial: "+224", flag: "🇬🇳" },
  { iso: "GW", name: "Guinea-Bissau", dial: "+245", flag: "🇬🇼" },
  { iso: "KE", name: "Kenya", dial: "+254", flag: "🇰🇪" },
  { iso: "LS", name: "Lesotho", dial: "+266", flag: "🇱🇸" },
  { iso: "LR", name: "Liberia", dial: "+231", flag: "🇱🇷" },
  { iso: "LY", name: "Libya", dial: "+218", flag: "🇱🇾" },
  { iso: "MG", name: "Madagascar", dial: "+261", flag: "🇲🇬" },
  { iso: "MW", name: "Malawi", dial: "+265", flag: "🇲🇼" },
  { iso: "ML", name: "Mali", dial: "+223", flag: "🇲🇱" },
  { iso: "MR", name: "Mauritania", dial: "+222", flag: "🇲🇷" },
  { iso: "MU", name: "Mauritius", dial: "+230", flag: "🇲🇺" },
  { iso: "MA", name: "Morocco", dial: "+212", flag: "🇲🇦" },
  { iso: "MZ", name: "Mozambique", dial: "+258", flag: "🇲🇿" },
  { iso: "NA", name: "Namibia", dial: "+264", flag: "🇳🇦" },
  { iso: "NE", name: "Niger", dial: "+227", flag: "🇳🇪" },
  { iso: "RW", name: "Rwanda", dial: "+250", flag: "🇷🇼" },
  { iso: "ST", name: "São Tomé and Príncipe", dial: "+239", flag: "🇸🇹" },
  { iso: "SN", name: "Senegal", dial: "+221", flag: "🇸🇳" },
  { iso: "SC", name: "Seychelles", dial: "+248", flag: "🇸🇨" },
  { iso: "SL", name: "Sierra Leone", dial: "+232", flag: "🇸🇱" },
  { iso: "SO", name: "Somalia", dial: "+252", flag: "🇸🇴" },
  { iso: "ZA", name: "South Africa", dial: "+27", flag: "🇿🇦" },
  { iso: "SS", name: "South Sudan", dial: "+211", flag: "🇸🇸" },
  { iso: "SD", name: "Sudan", dial: "+249", flag: "🇸🇩" },
  { iso: "TZ", name: "Tanzania", dial: "+255", flag: "🇹🇿" },
  { iso: "TG", name: "Togo", dial: "+228", flag: "🇹🇬" },
  { iso: "TN", name: "Tunisia", dial: "+216", flag: "🇹🇳" },
  { iso: "UG", name: "Uganda", dial: "+256", flag: "🇺🇬" },
  { iso: "ZM", name: "Zambia", dial: "+260", flag: "🇿🇲" },
  { iso: "ZW", name: "Zimbabwe", dial: "+263", flag: "🇿🇼" },
];

/** Default dialling code — Nigeria, the launch market. */
export const DEFAULT_DIAL = "+234";

export default function PhoneField({
  label = "Mobile number",
  hint = "For WhatsApp or SMS alerts. Enter your number without the leading zero.",
  required = false,
}: {
  label?: string;
  hint?: string;
  required?: boolean;
}) {
  return (
    <div className="authform__field">
      <label htmlFor="f-phone-local" className="authform__label">
        {label}
        {!required && <span className="authform__optional">optional</span>}
      </label>

      <div className="phonefield">
        {/*
          The country select is labelled separately for screen readers — without its own
          label it is announced only as "combo box", and a blind user cannot tell it apart
          from the language selector further down the form.
        */}
        <label htmlFor="f-phone-country" className="sr-only">
          Country dialling code
        </label>
        <select
          id="f-phone-country"
          name="phone_country"
          className="authform__input phonefield__code"
          defaultValue={DEFAULT_DIAL}
        >
          {AFRICAN_DIAL_CODES.map((c) => (
            // The value is the dial code, not the ISO code: it is what the Server Action
            // needs, so nothing has to look anything up. Several countries share a dial
            // code in other regions, but no two African codes collide, so this is safe
            // here — a global list would need the ISO code as the value instead.
            <option key={c.iso} value={c.dial}>
              {c.flag} {c.iso} {c.dial}
            </option>
          ))}
        </select>

        <input
          id="f-phone-local"
          name="phone_local"
          type="tel"
          inputMode="tel"
          autoComplete="tel-national"
          required={required}
          placeholder="803 123 4567"
          className="authform__input phonefield__number"
          aria-describedby="f-phone-hint"
        />
      </div>

      <p id="f-phone-hint" className="authform__hint">
        {hint}
      </p>
    </div>
  );
}
