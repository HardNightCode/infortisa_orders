pipeline {
  agent any
  options {
    ansiColor('xterm')
    timestamps()
    timeout(time: 15, unit: 'MINUTES')
  }
  environment {
    STAGE_HOST   = '10.0.100.160'
    ODOO_SERVICE = 'odoo'
    DB_NAME      = 'odoo_nexus'
    MODULE_NAME  = 'infortisa_orders'
  }
  stages {
    stage('SSH & Restart') {
      steps {
        sshagent (credentials: ['deploy']) {
          sh '''
            set -eu
            ssh -o StrictHostKeyChecking=no deploy@${STAGE_HOST} "whoami && hostname"
            ssh -o StrictHostKeyChecking=no deploy@${STAGE_HOST} "sudo -n systemctl restart ${ODOO_SERVICE} && sudo -n systemctl is-active --quiet ${ODOO_SERVICE}"
          '''
        }
      }
    }
    stage('Upgrade módulo en STAGE') {
      steps {
        sshagent (credentials: ['deploy']) {
          sh '''
            set -eu
            ssh -o StrictHostKeyChecking=no deploy@${STAGE_HOST} "sudo -n -u odoo /usr/bin/odoo -c /etc/odoo/odoo.conf -d ${DB_NAME} -u ${MODULE_NAME} --stop-after-init"
          '''
        }
      }
    }
    stage('Health check') {
      steps {
        sshagent (credentials: ['deploy']) {
          sh '''
            set -eu
            ssh -o StrictHostKeyChecking=no deploy@${STAGE_HOST} "curl -fsS http://localhost:8069/web/login >/dev/null"
            echo "STAGE OK"
          '''
        }
      }
    }
  }
}
