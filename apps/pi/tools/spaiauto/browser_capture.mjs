export default (sourceUrl) => {
  // Read-only DOM capture for tab.playwright.evaluate(captureFunction, queuedSourceUrl).
  // Scan first and inspect the result. This is not a headless browser runner.
  const text = (id) => document.getElementById(id)?.innerText?.trim() || "";
  const result = document.getElementById("ecommerceResult");
  if (!result || !result.getClientRects().length ||
      document.getElementById("btnExtractProduct")?.disabled || !text("productTitle")) {
    throw new Error("A completed, visible Spaiauto result is required.");
  }
  const input = document.getElementById("ecommerceInput")?.value || "";
  if (!input.includes(sourceUrl)) throw new Error("Queued URL is not in the current input.");
  const price = text("productPrice");
  return {
    schema_version: 1,
    acquisition_tool: "spaiauto",
    extractor_url: "https://tools.spaiauto.com/boc-tach-san-pham-shopee-tiktok",
    source_url: sourceUrl,
    captured_at: new Date().toISOString(),
    input_text: input,
    title: text("productTitle"),
    description: text("productFullDesc"),
    price_display: price,
    currency: /₫|\bVND\b/.test(price) ? "VND" : null,
    variations_text: text("productVariationsList"),
    platform_display: text("productPlatformBadge"),
    shop_display: text("productShop"),
    // URLs observed in this product result only; these are not downloaded files.
    video_urls: Array.from(new Set(Array.from(result.querySelectorAll("video, video source"))
      .map(node => node.currentSrc || node.src).filter(url => url && url.startsWith("https://")))),
    album_count_display: text("tabImagesCount"),
  };
}
