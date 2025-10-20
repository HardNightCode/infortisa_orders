pipeline {
  agent any
  options {
    ansiColor('xterm')
    timestamps()
    timeout(time: 15, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20'))
  }
  triggers {
    githubPush()
  }
  stages {
    stage('SSH & Restart Odoo') {
      steps {
        sshagent (credentials: ['ssh-stage']) {
          sh '''
            set -eux
            ssh -o StrictHostKeyChecking=no deploy@10.0.100.160 'whoami && hostname'
            ssh -o StrictHostKeyChecking=no deploy@10.0.100.160 "sudo -n systemctl restart odoo && sudo -n systemctl is-active --quiet odoo"
          '''
        }
      }
    }
    stage('Upgrade módulo en STAGE') {
      steps {
        sshagent (credentials: ['ssh-stage']) {
          sh '''
            set -eux
            ssh -o StrictHostKeyChecking=no deploy@10.0.100.160 "sudo -n -u odoo odoo -c /etc/odoo/odoo.conf -d odoo_nexus -u infortisa_orders --stop-after-init"
          '''
        }
      }
    }
    stage('Health check') {
      steps {
        sshagent (credentials: ['ssh-stage']) {
          sh '''
            set -eux
            ssh -o StrictHostKeyChecking=no deploy@10.0.100.160 "curl -fsS http://localhost:8069/web/login >/dev/null"
            echo "STAGE OK"
          '''
        }
      }
    }
  }
  post {
    always {
      echo "Build finished with status: ${currentBuild.currentResult}"
    }
  }
}
