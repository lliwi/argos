---
name: web-recon
description: Reconocimiento inicial y ordenado de un servicio web propio autorizado (perfil pentest).
version: 1.0.0
---
Solo actúas dentro del alcance autorizado del perfil; si un objetivo no está en él, no insistas:
dilo y para. Trabaja de menos a más intrusivo y para a resumir entre fases.

1. Confirma el alcance y la autorización antes de empezar. Si no hay `scope`/`authorization_ref`,
   no ejecutes nada: explícalo.
2. Identifícalo primero sin tocar apenas: `kali.whatweb` sobre la URL, y `kali.nmap` con `-sT -sV`
   a los puertos web habituales. Resume servicios y tecnologías en el scratchpad.
3. Superficie: `kali.gobuster` para rutas comunes; si es WordPress, `kali.wpscan`.
4. Vulnerabilidades: `kali.nikto`. Propón, pero no lances, pruebas más intrusivas (p. ej. sqlmap)
   sin confirmación explícita del usuario.
5. Entrega en `out/informe.md`: alcance, autorización, hallazgos con severidad y evidencia, y
   próximos pasos. No incluyas secretos ni datos de terceros.
