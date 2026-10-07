import { cp, mkdir } from "node:fs/promises";

for (const name of ["cmaps", "standard_fonts", "wasm"]) {
  const destination = new URL(`../public/pdf-assets/${name}/`, import.meta.url);
  await mkdir(destination, { recursive: true });
  await cp(
    new URL(`../node_modules/pdfjs-dist/${name}/`, import.meta.url),
    destination,
    { recursive: true },
  );
}
