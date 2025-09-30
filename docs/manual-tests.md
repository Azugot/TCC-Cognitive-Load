# Manual Test Plan

## Teacher removes classroom student

1. Autentique-se como professor responsável pela sala de teste.
2. Abra o painel "Gerenciar alunos" e tente remover um usuário que **não** aparece na lista de alunos.
   - ✅ Confirme que a interface apresenta o aviso `⚠️ Usuário não é aluno desta sala.` e que a lista permanece inalterada.
3. Em seguida, selecione um aluno que consta na lista e solicite a remoção.
   - ✅ Verifique que a operação conclui com a mensagem `✅ Aluno removido.` e que o aluno desaparece da listagem após a atualização.

> Estas etapas garantem que o feedback exibido ao professor reflita corretamente se o usuário participava ou não da sala antes da tentativa de remoção.
