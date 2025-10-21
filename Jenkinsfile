pipeline {
  agent any

  environment {
    // --- Ajustes comunes ---
    MODULE_NAME   = 'infortisa_orders'
    ODOO_BIN      = '/usr/bin/odoo'
    ODOO_CONF     = '/etc/odoo/odoo.conf'
    ADDON_DIR     = "/opt/odoo/custom/addons/${env.MODULE_NAME}"
    SERVICE_NAME  = 'odoo'
    DB_NAME       = 'odoo_nexus'
    // Palabras de error a buscar en journalctl
    ERR_PATTERNS  = "ERROR|Traceback|CRITICAL|psycopg2|odoo.exceptions|xmlrpc.client.Fault|IntegrityError"

    // --- STAGE ---
    STAGE_HOST    = '10.0.100.160'
    STAGE_USER    = 'deploy'
    STAGE_CREDS   = 'ssh-stage'   // ID credencial Jenkins

    // --- PROD ---
    PROD_HOST     = '10.0.100.152'
    PROD_USER     = 'deploy'
    PROD_CREDS    = 'ssh-prod'    // ID credencial Jenkins
  }

  options {
    ansiColor('xterm')
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '25'))
    disableConcurrentBuilds()
  }

  triggers {
    githubPush()   // usa tu webhook de GitHub
    // pollSCM('* * * * *')  // opcional
  }

  stages {

    stage('Checkout') {
      steps {
        echo "SCM: ${env.GIT_URL ?: 'git@github.com:HardNightCode/infortisa_orders.git'}"
      }
    }

    stage('Deploy & Verify STAGE') {
      steps {
        sshagent(credentials: [env.STAGE_CREDS]) {
          script {
            deployAndVerify(env.STAGE_USER, env.STAGE_HOST)
          }
        }
      }
    }

    stage('Promote to PROD (auto)') {
      when { expression { currentBuild.currentResult == 'SUCCESS' } }
      steps {
        sshagent(credentials: [env.PROD_CREDS]) {
          script {
            deployAndVerify(env.PROD_USER, env.PROD_HOST)
          }
        }
      }
    }
  }

  post {
    success {
      echo '✅ Pipeline OK: STAGE y PROD desplegados sin errores.'
    }
    failure {
      echo '❌ Pipeline FAILED. Revisa la etapa donde ocurrió y los logs impresos arriba.'
    }
  }
}

// ===================== Helpers =====================

// Ejecuta comandos en el host remoto via SSH (sin capturar salida)
def sshRun(String user, String host, String remoteScript) {
  sh """#!/usr/bin/env bash
set -euo pipefail
ssh -o StrictHostKeyChecking=no ${user}@${host} /bin/bash -s <<'REMOTE'
set -euo pipefail
${remoteScript}
REMOTE
"""
}

// Ejecuta comandos en el host remoto via SSH y devuelve stdout (trim)
def sshRunOut(String user, String host, String remoteScript) {
  return sh(script: """#!/usr/bin/env bash
set -euo pipefail
ssh -o StrictHostKeyChecking=no ${user}@${host} /bin/bash -s <<'REMOTE'
set -euo pipefail
${remoteScript}
REMOTE
""", returnStdout: true).trim()
}

// ================== Deploy + Verificación ==================

def deployAndVerify(String user, String host) {
  // Calcula el padre del directorio del add-on (sin usar $(dirname ...))
  def addonDir    = env.ADDON_DIR
  def addonParent = addonDir.contains('/') ? addonDir.substring(0, addonDir.lastIndexOf('/')) : '.'

  // Commit previo para posible rollback
  def prevCommit = sshRunOut(user, host, """
if [ -d "${addonDir}/.git" ]; then
  cd "${addonDir}"
  git rev-parse HEAD~1 2>/dev/null || true
fi
""")
  echo "Prev commit en ${host}: ${prevCommit ?: '(no disponible, primer deploy)'}"

  try {
    // --- 1) GIT: clonar/actualizar módulo ---
    sshRun(user, host, """
if [ ! -d "${addonDir}" ]; then
  mkdir -p "${addonParent}"
  cd "${addonParent}"
  git clone git@github.com:HardNightCode/${env.MODULE_NAME}.git
fi
cd "${addonDir}"
git fetch --all --prune
git reset --hard origin/main
git rev-parse HEAD
""")

    // --- 2) Upgrade de módulo en Odoo ---
    sshRun(user, host, """
sudo -n -u odoo ${env.ODOO_BIN} -c ${env.ODOO_CONF} -d ${env.DB_NAME} -u ${env.MODULE_NAME} --stop-after-init
""")

    // --- 3) Restart servicio + health check ---
    sshRun(user, host, """
sudo -n systemctl restart ${env.SERVICE_NAME}
sudo -n systemctl is-active --quiet ${env.SERVICE_NAME}
curl -fsS http://localhost:8069/web/login >/dev/null
echo 'Health OK'
""")

    // --- 4) Logs y detección de errores ---
    // Hacemos todo en remoto para evitar más quoting
    sshRun(user, host, """
echo "==== Últimas 500 líneas de journalctl en ${host} ===="
sudo -n journalctl -u ${env.SERVICE_NAME} -n 500 --no-pager || true
echo "==== Grep de errores en ${host} (si coincide, fallará) ===="
if sudo -n journalctl -u ${env.SERVICE_NAME} -n 500 --no-pager | egrep -i '${env.ERR_PATTERNS}'; then
  echo 'Se detectaron errores en logs.'
  exit 1
fi
""")

    echo "✅ ${host}: Deploy y verificación OK"

  } catch (err) {
    echo "❌ ${host}: FALLO detectado. Iniciando ROLLBACK…"
    if (prevCommit) {
      // 5) Rollback al commit anterior y reintento rápido
      sshRun(user, host, """
cd "${addonDir}"
git reset --hard ${prevCommit}
sudo -n -u odoo ${env.ODOO_BIN} -c ${env.ODOO_CONF} -d ${env.DB_NAME} -u ${env.MODULE_NAME} --stop-after-init
sudo -n systemctl restart ${env.SERVICE_NAME}
sudo -n systemctl is-active --quiet ${env.SERVICE_NAME}
curl -fsS http://localhost:8069/web/login >/dev/null
""")
    } else {
      echo "⚠️ ${host}: No hay commit previo para rollback."
    }

    // 6) Dump de logs extenso para diagnóstico
    sshRun(user, host, """
echo "==== LOGS tras el error en ${host} (últimas 800 líneas) ===="
sudo -n journalctl -u ${env.SERVICE_NAME} -n 800 --no-pager || true
""")

    error("Abortando pipeline por fallo en ${host}.")
  }
}
